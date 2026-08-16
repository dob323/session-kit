# Claude Code and Codex integration

Session Kit manages Claude Code, Codex, or both from one local session
inventory. The providers continue to own credentials and conversation data;
the kit supplies isolation, identity proof, names, colours, and guarded launch
and resume paths.

## Account isolation

Each enrolled Claude account has its own `CLAUDE_CONFIG_DIR`. Each enrolled
Codex account has its own `CODEX_HOME`. Session Kit selects the configured root
when it launches or resumes a provider and records only the local alias and
verified account description needed to show the choice.

It does not copy credentials between profiles, put tokens in its account
registry, or print them in picker and detail output.

## Exact provider identity

Claude identity comes from structured agent state joined to the exact native
process tree. Codex identity comes from the native Codex process and its
structured conversation record: an open rollout on Linux, and the process's
`CODEX_THREAD_ID` matched to one owner-controlled rollout on macOS.

Directory, title, timestamp, and terminal text are display context. A missing,
duplicated, changed, or malformed provider identity produces `pending` and
blocks mutation. A visible terminal is not enough proof to choose a
conversation.

## Task names

Provider instructions may ask a new managed root conversation to name itself:

```text
At the first substantive request in a new Session Kit-managed root
conversation, before spawning child agents, run:

  sp self-name "<2-5 word Task Focused Title>"

If exact identity is not ready, retry once on the next root turn. Do not run
this command from a child agent.
```

`sp self-name` accepts a two-to-five-word Title Case name and proves the
managed root identity before and after writing it. A manual name always wins
and permanently blocks an automatic replacement. Set
`SESSION_KIT_AUTO_NAME=0` to stop new automatic names without deleting any
already retained.

Claude can claim a name at the first title-hook event. Codex must wait until
its first turn creates a conversation identity. A durable claim prevents a
second hook, later inventory pass, or restart from renaming the same
conversation again.

`session-kit doctor` checks provider instructions for `sp self-name`. For
Claude it also checks the installed naming hook and its `SessionStart`,
`UserPromptSubmit`, and `Stop` registrations.

## Titles and provider chrome

Claude stores its generated title separately from the name shown in its prompt
box. Before a human-facing inventory, Session Kit fills an absent native name
from the retained title and leaves an explicit `/rename` unchanged. A running
Claude window may not repaint immediately; `sp detail` then says the title
`waits for the session to restart` instead of adding another state to the
picker row.

Codex receives its terminal-title items and Session Kit theme as per-launch
configuration. The kit does not edit `~/.codex/config.toml`. A new Codex
process may begin before its conversation title exists; the stored title is
applied through the guarded resume path once exact identity is available.

Claude's supported colour names are red, blue, green, yellow, purple, orange,
pink, and cyan. Codex loads the kit's separate theme files. Keeping the
palettes disjoint makes the colour an additional provider cue. See
[Display setup](usage.md#display-setup) for the installed files and terminal
behaviour.

## State evidence

Session Kit derives screen state from structured evidence rather than prose or
punctuation.

For Codex:

- an unresolved `request_user_input` without `autoResolutionMs` means
  `needs you`;
- a picker with `autoResolutionMs` is optional;
- matching tool output resolves the request;
- a new task supersedes an unanswered picker from an earlier task;
- completed or aborted tasks do not keep an attention state.

Codex remains `needs you` because its currently read app-server records do not
prove that a picker or approval is open at this instant.

Claude `question` requires an unmatched top-level `AskUserQuestion`, or an
unresolved top-level tool use correlated by timestamp with the current
permission-prompt hook. Sidechains are excluded. Other structured Claude
attention is `needs you`. A state word the mapping does not understand becomes
`pending`, never raw provider copy.

A needs-you session becomes `idle` only after its transcript path, size, and
nanosecond modification time remain unchanged for the configured idle window.
This evidence is provider-neutral and is unrelated to a vendor's own `idle`
notification.

## Provider exit

Claude and Codex run as children of the managed session shell. A clean provider
exit means the person is finished: the shell records the recoverable
conversation, closes the session, and returns its number to quarantine. Closed
sessions offers Restore.

A crash is different. The shell reopens the exact conversation once and says
so. A second crash within a minute stops the loop. The session closes only when
the conversation is proved recoverable; otherwise it remains open, returns to
the picker when possible, and states why. There is no four-choice exit menu.

The provider command `/kit` is the deliberate way to leave a healthy
conversation running and return to the picker. `/exit` closes it.

## Resume and fork

The provider forms carried by the guarded launch path are:

```text
claude [--name <stored-title>] --resume <exact-uuid>
codex --no-alt-screen resume <exact-uuid>
claude --resume <exact-source-uuid> --fork-session
codex --no-alt-screen fork <exact-source-uuid>
```

Session Kit verifies the source identity and the resulting provider UUID. An
ambiguous, already-active, or changed identity is refused.

## Changing an account

An account change resumes the same conversation; it is not a fork. Session Kit
retains its provider UUID, history, title, project, colour, and boot-scoped
session number. Before stopping anything, it proves that the exact provider
generation has no active turn, tool, hook, subagent, child agent, or background
work, then checks again under the action lock.

The target profile must be enrolled, verified, healthy, and signed in. The kit
checkpoints the exact conversation data, resumes the same UUID under the target
profile, and records the new alias only after resulting identity is proved. A
failed resume restores the checkpoint and attempts the original profile; an
unproved rollback fails closed.

Older terminals created before account-aware launch records may need a one-time
managed-shell recreation. That migration is explicit and retains the exact
conversation and display identity.

Automatic account switching is opt-in and still bounded to a verified,
idle-enough conversation and a configured reserve. It never enables an account
or silently changes models.

## Compatibility

Provider local formats and commands can change. Each release records the
provider versions, operating system, and architecture used in its acceptance
checks. Combinations outside that evidence are best-effort until verified.
The private-format assumptions and their visible failure modes are listed in
[Vendor formats](pinned-internal-formats.md).
