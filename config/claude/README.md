# Claude Code integration files (canonical copies)

These files make a Session Kit name visible inside a Claude Code window, and one
of them tells the kit when a session starts waiting for a person.
`nameintent_title.sh` and `statusline.sh` are installer-managed: install,
update, and rollback copy them from the selected release, and uninstall removes
an unchanged kit copy and its exact registrations. `attention_hook.sh` is the
exception -- it is hand-deployed, so no install, update, or rollback copies it
and no uninstall removes it. Every copy here is canonical: `session-kit doctor`
compares installed content to it, and an edit to a live file has to be mirrored
back or the next person reads a stale source.

| repo copy | live file |
|---|---|
| `nameintent_title.sh` | `~/.claude/hooks/nameintent_title.sh` |
| `attention_hook.sh` | `~/.claude/hooks/attention_hook.sh` |
| `statusline.sh` | `~/.claude/statusline.sh` |

`session-kit doctor` checks the naming hook as `naming-hook` and the status line
as `statusline`. Both files must be owner-only executable regular files whose
bytes match the active release. The hook must be registered for all three of its
events (see [Hook registration](#hook-registration)) and produce the expected
title JSON against a throwaway fixture `HOME`; the status line must have its
exact registration. Every enrolled Claude account profile is checked because
`CLAUDE_CONFIG_DIR` replaces the default settings. A missing piece is a warning,
not a failure.

## How the kit learns a session is waiting for you

`claude agents --json` is a poll: it says what was true when it ran, and it is
the most expensive part of a snapshot, so it cannot simply run more often. A
question that appears between two polls is invisible until the next one.

`attention_hook.sh` closes that gap. Claude Code fires its Notification hook at
the moment attention is wanted, and the hook writes one small record per
session under
`${SESSION_KIT_STATE_DIR:-~/.local/state/session-kit}/attention/claude/<uuid>.json`.
The picker reads that record beside the poll and takes whichever evidence is
newer; the poll stays as reconciliation, so an estate with no hook installed
behaves exactly as it did before. Register it for three events:

```bash
install -D -m 700 \
  ~/.local/lib/session-kit/current/config/claude/attention_hook.sh \
  ~/.claude/hooks/attention_hook.sh
```

Then add the three registrations to the applicable Claude settings file:

```json
"Notification":     [{"matcher": "", "hooks": [{"type": "command", "command": "~/.claude/hooks/attention_hook.sh", "timeout": 10}]}],
"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "~/.claude/hooks/attention_hook.sh", "timeout": 10}]}],
"SessionEnd":       [{"hooks": [{"type": "command", "command": "~/.claude/hooks/attention_hook.sh", "timeout": 10}]}]
```

`session-kit doctor` reports it as `attention-hook`: the file must be an
owner-only executable regular file, registered for all three events, and it has
to produce a real record when doctor feeds it a fixture notification. Not
installed at all is a warning that names what you lose, not a failure.

Two settings decide how far this goes. `SESSION_KIT_ATTENTION_SOURCE=poll` is
the kill switch, the hook keeps writing, nothing reads it, and the picker is
back to the poll-only reading it shipped with. `hook` is the opposite extreme
and exists for drills. The default, `auto`, merges the two.

Which notification types mean "a person is waited on" is decided in one place
(`lib/sessionkit_inventory/attention.py`) and pinned to Claude Code's own
matcher enum by `tests/test_attention_truth.py`. A type the kit does not
recognise is deliberately left alone: the poll still sees that session, so a new
vendor event costs a refresh of latency rather than a wrong answer.

## How a session name reaches a Claude window

The kit pushes a name onto three surfaces: `~/.claude/sessions/<uuid>.nameintent`,
an `agent-name` record appended to the matching transcripts, and the per-PID
`~/.claude/sessions/<pid>.json` record whose `sessionId` matches the
conversation. The three feed different parts of the interface, and only the
first is read by the hook.

Claude Code applies an external `sessionTitle` from hook output at exactly two
moments in the supported provider behavior: **SessionStart** and
**UserPromptSubmit**. `Stop` and `PostToolUse` output is ignored. A window that
is only being watched therefore keeps its old tab title and top-right chip until
the human sends the next prompt; the name is not lost, it is queued behind an
event that has not happened yet.

The status line is the one surface that repaints live mid-turn. It renders the
nameintent name at the start of line 1, tinted with the session's kit color.
Register it with a refresh timer so idle windows update too:

```json
"statusLine": {
  "type": "command",
  "command": "~/.claude/statusline.sh",
  "refreshInterval": 2
}
```

Three things about that script are worth knowing before deploying it. It reads
`display_color` out of `~/.local/state/session-kit/inventory.json` by a literal
path, so an installation that moves its state root with `SESSION_KIT_STATE_DIR`
or `XDG_STATE_HOME` gets the untinted fallback rather than an error. Its color
table covers exactly the eight names in the Claude palette and nothing else,
which is complete rather than partial: the row is looked up by Claude Code's own
`session_id`, so only a Claude color can ever reach it, and the six Codex-only
names cannot. And it declares itself the Linux status line: line 2 uses GNU
`stat -c`.

### Line 2: quota refresher extension

The quota display is an extension point. With no executable
`~/.claude/statusline-quota-refresh.sh`, line 2 reads `quota --`; the name,
model, location, context usage, and account display keep working. Session Kit
does not make a quota request itself. A contract skeleton ships as
`extras/statusline-quota-refresh.example`; it deliberately leaves the endpoint
and credential-bearing transport as a marked TODO.

The status line invokes a refresher in the background at most once every 180
seconds. An ambient account runs it normally. An enrolled account runs it with
`HOME` set to an isolated probe directory and `CLAUDE_CONFIG_DIR` set to that
profile; the probe home links that profile's `.claude.json` and
`.credentials.json`. The refresher must therefore use the `HOME` it receives,
not assume the operator's normal home.

The refresher writes `$HOME/.claude/cache/quota_headers` as `key: value` lines.
The required fields are:

- `x-probe-account`: the email from that invocation's `.claude.json`; a
  mismatch is discarded rather than shown for the wrong account;
- `anthropic-ratelimit-unified-5h-utilization`: a fraction from 0 to 1;
- `anthropic-ratelimit-unified-5h-reset`: a Unix epoch time in seconds.

The matching `anthropic-ratelimit-unified-7d-utilization` and `-7d-reset`
fields are optional, but must appear as a pair. The status line copies a valid
result into its account-and-profile-specific directory under
`~/.claude/cache/session-kit-quota/`; the refresher does not write there.

After filling in the TODO, install a private executable copy:

```bash
install -m 700 ~/.local/lib/session-kit/current/extras/statusline-quota-refresh.example ~/.claude/statusline-quota-refresh.sh
```

Windows that boot before their name exists are restarted-as-resumed once at
reopen by the provider bounce (`picker_bounce_claude` in
`lib/sh/sp_provider_bounce.sh`, sourced by `bin/sp`), which boots them through
SessionStart with the name already applied. The bounce is heavily guarded, since
restarting a provider that is mid-thought would lose work: it runs only against
a detached ready session with no subagents, whose provider is idle or awaiting a
reply, that has produced no output for at least 120 seconds, and whose PID and
process start time still match the frozen proof. It also asks the kit whether a
real prompt already followed the name intent, if one did, the live window has
the name, so the pending marker is dropped and nothing restarts.

## Hook registration

`nameintent_title.sh` must be registered in `~/.claude/settings.json` for
`SessionStart`, `UserPromptSubmit`, and `Stop`. The first two are the events
that can retitle a live window. `Stop` cannot, and the hook emits nothing there;
it is registered because Stop is where the kit derives an automatic title for a
resume-launched conversation that Claude never auto-titled, writing the intent
file that renames the window at the next prompt.

The hook fails open. Any unexpected input, missing file, or failed lookup exits
0 with no output, which leaves the window titled as it was.

## Why the vendor's off-switch is never used

`CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1` looks like the clean way to stop Claude
Code writing its own tab titles so the kit can own the tab outright. It is not.
Compatibility checks show that it suppresses every OSC title sequence,
including a title supplied by hook output:

| run | switch | what the window wrote |
| --- | --- | --- |
| hook returns `sessionTitle` | unset | `ESC]0;✳ <kit title>BEL` |
| hook returns `sessionTitle` | `=1` | nothing |
| hook returns `terminalSequence` | unset | `ESC]0;✳ <kit title>BEL` |
| hook returns `terminalSequence` | `=1` | nothing |

The switch silences the window's title writes entirely, hook output included,
so setting it would leave every kit-launched Claude window with no tab name at
all. The kit therefore leaves it alone, writes the tab name itself when a
session is entered, `sp go`, `sp takeover`, a fresh `sp new`, and the picker's
own open and take-over, all through `sk_tab_title` in `bin/session_kit_common`
, and lets this hook carry renames from inside the window. A restore writes no
tab name (it ends at the picker or a prompt, not inside the session); it writes
the name and colour into the provider's store, and the tab is named when the
session is opened. `session-kit doctor` reports the variable under `tab-title`
if it finds it set. The whole behavior, both providers, turns off with
`SESSION_KIT_TAB_TITLE=off`.
