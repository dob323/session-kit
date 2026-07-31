# Claude Code and Codex integration

Session Kit can manage Claude Code, Codex, or both in one shpool inventory.

## Exact provider identity

Claude Code identity comes from structured Claude agent state joined to the
exact Linux process tree.

Codex identity comes from the native Codex root process and its open CLI
rollout, joined to local metadata when available.

Working directory, title, timestamp, and terminal output are never enough to
choose a conversation. A missing, duplicated, or malformed provider identity
is shown as unavailable and cannot be mutated.

## Task names

A provider's user-level instructions may ask a new managed root conversation
to assign a short title:

```text
At the first substantive request in a new Session Kit-managed root
conversation, before spawning child agents, run:

  sp self-name "<2-5 word Task Focused Title>"

If exact identity is not ready, retry once on the next root turn. Do not run
this command from a child agent.
```

Review the current provider documentation before changing its instruction
files.

`sp self-name` accepts a 2–5 word Title Case name and verifies the managed root
identity before and after the write. Manual names take priority. Set
`SESSION_KIT_AUTO_NAME=0` to stop new automatic naming without deleting retained
titles.

## Reply state

Session Kit marks `needs your reply` only when structured provider state proves
that a question is unresolved.

For Codex:

- unresolved `request_user_input` without `autoResolutionMs` needs a reply;
- a picker with `autoResolutionMs` is optional;
- the matching tool output resolves the question;
- a new task supersedes an unanswered picker from the earlier task;
- completed or aborted tasks do not keep an alert.

The classifier does not search prose for question marks. An incomplete or
malformed event window produces an unavailable state instead of a guess.
Claude Code reply state comes from its structured agent inventory.

## Provider exit

Claude Code or Codex runs as a child of the managed terminal shell. A normal
provider exit returns to that shell and records an exited-provider state. It
does not destroy the shpool terminal.

Opening the provider-exited terminal presents four choices:

- reopen the exact conversation;
- mark the terminal to keep;
- open an ordinary shell, which permanently excludes that terminal from
  automatic cleanup;
- close the terminal.

The dashboard must not describe an exited provider as actively running.

## Resume and fork

Exact resume forms are:

```text
claude --resume <exact-uuid>
codex --no-alt-screen resume <exact-uuid>
```

Independent forks use:

```text
claude --resume <exact-source-uuid> --fork-session
codex --no-alt-screen fork <exact-source-uuid>
```

Session Kit verifies both the source and resulting provider UUID. It refuses an
ambiguous, already-active, or changed identity.

## Compatibility

Provider local formats and command interfaces can change. Each beta release
must publish the exact Claude Code and Codex versions used in install, reply,
exit, resume, recovery, and fork tests. Versions outside that release evidence
are best-effort until verified.
