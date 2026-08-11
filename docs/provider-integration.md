# Claude Code and Codex integration

Session Kit can manage Claude Code, Codex, or both in one shpool inventory.

## Account isolation

Each enrolled Claude Code account runs with its own `CLAUDE_CONFIG_DIR`. Each
enrolled Codex account runs with its own `CODEX_HOME`. This uses the providers'
native configuration boundaries: Session Kit selects a root when it launches or
resumes the provider, while the provider continues to own its login credentials
and conversation storage.

Session Kit records only the local alias and account description needed to show
and verify the choice. It does not copy, link, display, or log authentication
tokens, and it does not put them in the account registry.

## Exact provider identity

Claude Code identity comes from structured Claude agent state, joined to the
exact native platform process tree.

Codex identity comes from the native Codex root process and its open CLI rollout
on Linux. On macOS it comes from that exact process's `CODEX_THREAD_ID` and the
single owner-controlled local rollout whose structured UUID matches it, so macOS
does not depend on Linux open-file-descriptor traversal. The matched rollout also
supplies structured turn and reply state.

Working directory, title, timestamp, and terminal output are never enough to
choose a conversation. A missing, duplicated, or malformed provider identity is
shown as unavailable and cannot be mutated. This is why a command sometimes
refuses to act on a session you can plainly see: what is on screen is display
context, and the action needs identity.

## Task names

A provider's user-level instructions may ask a new managed root conversation to
name itself:

```text
At the first substantive request in a new Session Kit-managed root
conversation, before spawning child agents, run:

  sp self-name "<2-5 word Task Focused Title>"

If exact identity is not ready, retry once on the next root turn. Do not run
this command from a child agent.
```

Review the current provider documentation before changing its instruction files.

`sp self-name` accepts a 2–5 word Title Case name and verifies the managed root
identity both before and after the write. Manual names take priority: a session
someone renamed by hand refuses `sp self-name` and every other automatic rename,
permanently. Set `SESSION_KIT_AUTO_NAME=0` to stop new automatic naming without
deleting titles already retained.

Automatic naming takes a thread's name once, at its first prompt. For Claude
that is the title hook's first firing; for Codex it is the first pass after the
thread's first turn, which is the earliest moment a Codex thread has an identity
at all — a freshly created thread has no id and no first message until then. The
claim is recorded next to the aliases, so the hook's second firing, the next
inventory build, and the first pass after a restart all find the thread already
named.

The read-only doctor check reports when either provider's instruction file omits
`sp self-name`. For Claude it also verifies that the owner-controlled
`nameintent_title.sh` hook is executable and registered for `SessionStart`,
`UserPromptSubmit`, and `Stop`. Session Kit reports missing coverage; it does not
edit user-level provider instructions or hooks on your behalf.

## Titles Claude keeps in two places

Claude keeps its generated `ai-title` and its visible prompt-bar `agent-name` as
separate records. Before a human-facing inventory, Session Kit copies a missing
auto-title into the native name record and leaves any explicit `/rename`
unchanged.

Claude does not repaint an already-running TUI after an external record update,
so the row stays `title pending` until that exact conversation starts again.
Reopening an exited Claude provider passes the stored name through Claude's
native `--name` option.

## Colors, and a constraint that comes from Claude Code

Claude Code's `/color` command accepts exactly eight names: red, blue, green,
yellow, purple, orange, pink, and cyan. This was measured rather than assumed —
twenty-two names were driven through the same path Session Kit uses, with
known-good and known-bad controls. Every other name is rejected, and `gray` and
`grey` resolve to `default`, which is the absence of a color rather than a ninth
one.

Codex is the opposite: it loads a theme file from disk by name and applies no
allow-list, so its palette is Session Kit's to choose, and the kit ships one
theme file per Codex color.

That asymmetry is why the two providers have separate palettes rather than one
shared list. It also means the Claude palette cannot be widened from this side:
doing so would assign a Claude session a color Claude Code refuses at the moment
it matters, leaving the window with no color at all. See
[Session colors](usage.md#session-colors) for the resulting behavior.

New Claude sessions receive their stable color in a short hidden bootstrap
before the visible provider starts, and the timeout wrapper preserves that
bootstrap's standard input on both Linux and macOS. If the bootstrap cannot be
proved, creation fails open: Session Kit writes a missing native color for the
next exact start rather than failing the session, and never replaces a user's
own `/color` choice.

## Reply state

Session Kit marks `needs your reply` only when structured provider state proves
a question is unresolved. It does not search prose for question marks, and an
incomplete or malformed event window produces an unavailable state rather than a
guess.

For Codex:

- an unresolved `request_user_input` without `autoResolutionMs` needs a reply;
- a picker with `autoResolutionMs` is optional;
- the matching tool output resolves the question;
- a new task supersedes an unanswered picker from an earlier task;
- completed or aborted tasks do not keep an alert.

Claude Code reply state comes from its structured agent inventory.

## Provider exit

Claude Code and Codex run as children of the managed terminal shell. A normal
provider exit returns to that shell and records an exited-provider state; it does
not destroy the shpool terminal.

Opening a provider-exited terminal offers four choices: reopen the exact
conversation, mark the terminal to keep, open an ordinary shell (which
permanently excludes that terminal from automatic cleanup), or close the
terminal. The dashboard must never describe an exited provider as actively
running.

## Resume and fork

Exact resume forms:

```text
claude [--name <stored-title>] --resume <exact-uuid>
codex --no-alt-screen resume <exact-uuid>
```

Independent forks:

```text
claude --resume <exact-source-uuid> --fork-session
codex --no-alt-screen fork <exact-source-uuid>
```

Session Kit verifies both the source and the resulting provider UUID, and
refuses an ambiguous, already-active, or changed identity.

## Changing the account for an existing conversation

An account change is an exact resume, not a fork and not a new conversation.
Session Kit preserves the provider conversation UUID and its history, the title,
project, Session Kit color, and boot-scoped terminal number. Before stopping a
provider it proves that exact generation has no active turn, tool, hook,
subagent, child agent, or background provider work. Waiting for the operator's
reply is safe; work continuing without the operator is not. It checks again
under the action lock and requires a separate confirmation for every change.

The target profile must be enrolled, verified, healthy, and signed in. Session
Kit briefly stops the exact provider, transfers only the exact conversation
artifacts, resumes the same UUID under the target profile, and verifies the
resulting provider identity before recording the new alias. If resume or
verification fails, it restores the checkpoint and binding and tries to resume
the exact UUID under the original profile. An ambiguous identity or an unproven
rollback is reported and fails closed.

A managed terminal created by an account-aware release keeps its shell and
terminal attachment during this change. A terminal created before account
profiles existed has no account-aware launcher to return to. Its first account
change therefore requires an explicit one-time recreation of that managed shell;
the original window briefly reconnects, while the exact UUID, history, title,
project, color, and terminal number are retained. This legacy migration is never
started automatically.

Session Kit does not automatically rotate an existing conversation between
subscription accounts. Matrix advice can inform the choice, but a person must
confirm the account every time.

## Compatibility

Provider local formats and command interfaces can change, and Session Kit reads
both. Each beta release publishes the exact Claude Code and Codex versions,
operating system, and architecture used in its install, reply, exit, resume,
recovery, and fork tests. Versions and platform combinations outside that
release evidence are best-effort until verified.
