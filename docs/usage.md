# Use Session Kit

## The picker

SSH opens an ordinary shell. Type `kit` to open the picker. With shell
integration enabled, Linux Bash may also print a short `kit: open sessions`
hint; neither Bash nor macOS zsh starts the picker until you ask.

The picker is one list. Sessions **ready** for this window come before sessions
**open elsewhere**. Inside each group the state order is `question`, `needs
you`, `working`, then `idle`; machine sessions sit behind one counted row.
New session, Projects, Closed sessions, and Help follow the session rows.

```text
Enter           open the top or highlighted row; on an empty list, start new
b               go back; from the home screen, leave for an ordinary shell
digits          mark sessions: 5 · 1,4 · 7-9
letters         filter the list as you type
q               leave from the home screen when no filter text is typed
Esc             clear text, step back, then leave when nothing else remains
Ctrl-C          abandon the line being typed
Ctrl-D          leave from anywhere
↑ / ↓           move the highlight on the cursor-driven screen
mouse           scroll, highlight, and open on the cursor-driven screen
```

Nothing on the screen asks you to confirm anything. An action runs and then
says what it did, `Closed 3 sessions.`, and a close is recoverable from
Closed sessions. [Picker navigation](picker-navigation.md) covers the list,
marking, filtering, the mouse, and the action panel in full.

Opening a session that is open elsewhere defaults to moving it here after a
fresh identity check. The earlier window returns to its picker and the
conversation is never duplicated. New session defaults to Claude Code. The
action panel for a Claude or Codex session also offers account, model, history,
name, colour, close, and, where applicable, restore actions.

Picker rows and ordinary action lines hide shpool IDs and conversation UUIDs.
`sp detail` and the JSON modes expose exact identity for diagnosis. Commands
accept an ID wherever they accept a number, so a script that already holds one
keeps working.

### Keys in the key-driven picker

The installed `kit` command starts this key-driven surface directly. An
interactive Bash shell that loaded Session Kit's shell integration can instead
route its `kit` function through the cursor-driven launcher when that feature
is enabled; see [Troubleshoot](troubleshooting.md#which-picker-kit-opens).
The key-driven surface accepts:

```text
Enter           open the top row, or start new when the list is empty
number          open a session, or show what can be done with one open elsewhere
h number        read a session's history without opening it
n               new session
k numbers       close sessions: k 5 · k5 · k 5, 6, 8 · k 4-7 · k all
name number     give a Claude or Codex conversation a name of your own
name reset number    drop that name and show the provider's again
fork number     start a separate fork of a Claude or Codex conversation
model number    move an idle Claude or Codex conversation to another configured model
x               show or hide machine sessions behind their counted row
a               everything that needs you
g               move to the next session that needs you
d number        dismiss a repair that failed, from the Needs you screen
/text           filter names, providers, accounts, models, and projects as you type
group           group by state, provider, or project
c               compact rows: no headings, more sessions on screen
next / prev     move between pages; > and < do the same
r               refresh the list and clear the filter
m               more: sessions outside the kit, closed sessions, projects
p               projects: add, review, hide, or restore new-session directories
o               show sessions outside the kit, read-only
u               restore a closed conversation
?               show this table from the picker itself
b / q           leave the home screen for a regular shell
quit / exit     the same, by name
```

A key takes its number with or without the space: `k5` and `k 5` are the same
close. Closing keeps the typed number through the result and refresh. `?`
prints the same table from the picker itself, so the two cannot drift apart.

#### Why the key-driven picker's screen clears while you type

An idle picker keeps itself current. It collects a snapshot in the background
every few seconds and repaints only when something visible changes, so it never
runs in front of your prompt. Session numbers are stable, so a repaint never
changes what a number means, and the active search and page survive one.

A repaint clears the screen. Characters you typed without pressing Enter live in
the terminal's own input queue, which this process cannot inspect: in canonical
mode the kernel reports an incomplete line as no input at all, on both Linux and
macOS. So a repaint landing mid-command erases the *echo* of what you typed
while the characters themselves stay queued and intact. Press Enter and the
command you started still runs.

Set `SESSION_KIT_PICKER_REFRESH_SECONDS=0` to switch this off, or to a number of
seconds (minimum 2) to change the pace.

When activation selects a newer release, a running picker reloads the new
launcher at a safe refresh point and restores its filter, page, and selection.
If the new launcher cannot start, the current picker stays in place and reports
the degraded target.

## Command help

```text
sp help              every command, grouped, with a line each
sp help <topic>      one area in detail
sp --help, sp -h     the same reference
```

`sp help` answers before anything else is required, including on a machine
where shpool is missing or half-installed, that is the moment the instructions
are most needed. The topics are `sessions`, `names`, `accounts`, `history`,
`selectors`, `unavailable`, `exit-codes`, `completion`, and `machine`.

Every Session Kit command follows the same rule: `-h` and `--help` print their
reference to standard output and exit 0, and an argument mistake prints usage to
standard error and exits 2. `session-kit help` covers installation and
maintenance; `sp help` covers day-to-day session work.

The verbs beginning `picker-`, plus `sp restore-exact`, are not part of that
human list. The picker and the managed shell call them with proof arguments a
person does not hold; `sp help machine` names them for anyone reading the code.

`shpool_status --help` separates the modes that never touch disk from the ones
that refresh the cached inventory, fill in pending provider titles, or change
recovery state, several of them look like reads and are not.

## Tab completion

Session Kit installs one Bash completion file under each command name that
takes arguments:

```text
~/.local/share/bash-completion/completions/sp
~/.local/share/bash-completion/completions/session-kit
~/.local/share/bash-completion/completions/shpool_status
~/.local/share/bash-completion/completions/shpool_reaper
~/.local/share/bash-completion/completions/codex_resume_here
```

With the `bash-completion` package present, a new shell loads the right file on
demand. Without it, source one copy from your own startup file:

```bash
source ~/.local/share/bash-completion/completions/sp
```

It completes verbs, help topics, flags, provider names, color names, and the
aliases in your projects file. It never runs a Session Kit command and never
asks the manager for state, so a Tab press cannot change anything or stall on a
snapshot. That is also why it does not complete session numbers: the only
honest source for those is a live inventory, and building one on Tab would be a
state change nobody asked for.

Set `SESSION_KIT_COMPLETION_DIR` to install the copies elsewhere, or to `none`
to skip them. Uninstall removes only files that still carry the kit's own marker
line, so an edited copy is left alone.

## Exit codes

Every Session Kit command uses the same statuses.

| Status | Meaning |
| --- | --- |
| 0 | the command did what it was asked to do |
| 1 | refused or failed; the reason is on standard error |
| 2 | wrong arguments, or a selector that matched no session |
| 3 | live evidence was refused as stale, partial, or ambiguous (`shpool_status --strict-json`, `--guard-json`), or `shpool_reaper` found its disable sentinel |
| 64 | the launcher was invoked under a name that is not a Session Kit command |
| 69 | something required is unavailable: the current-release link, a helper missing from the pinned release, or Bash 4 or newer on macOS |
| 74 | the picker refused the action it was asked to take |
| 75 | the picker's attach failed after the action was allowed |
| 76 | a reopen ran after a provider crash and the reopened provider exited non-zero; the session closes if its conversation can be brought back, and otherwise stays open with the reason on screen |
| 78 | the launcher found a release or manager link outside the install root |
| 127 | `codex_resume_here` could not find the `codex` command |
| 143 | `sp` was terminated (SIGTERM or SIGHUP) |

A refusal is a status, not a suggestion: nothing partial has happened. Session
Kit disables a whole action rather than performing part of it.

## Start a session

```text
sp new claude [project-alias]
sp new codex [project-alias]
sp new shell [project-alias]
sp new <project-alias>
sp new claude [project-alias] --account <alias>
sp new codex [project-alias] --model <model-id>
sp new codex [project-alias] --origin machine
```

The last form uses the provider stored for that project alias. When the
project has a committed `session-kit.toml` and is on your project list, its
provider, account, and model are used unless a flag here overrides them; see
[Projects](projects.md).

Guided New asks for the provider, project, and account before launch. When Matrix
has current, healthy advice that names the same provider, its recommended account
is preselected and Enter confirms it. Missing, stale, unhealthy, logged-out, or
provider-mismatched advice leaves the account unselected. Codex currently has no
preselection because the available Matrix advice is not provider-qualified for
Codex; choose its account manually. This fail-closed behavior prevents advice
for one provider from selecting an account for the other.

`--model` starts the provider on that model; the identifier is validated for
the provider before anything is launched. `--origin machine` stamps the session
as a machine session, so it stays out of the picker's default list and lives
behind the counted row instead; `SESSION_KIT_ORIGIN=machine` in the launching
environment does the same.

A session started from **inside another session** is a machine session even
without either of those, because that is how agents start sessions and none of
them says so. If you run `sp new` in one of your own windows and want the new
session in your own list, say `--origin human`. Everything the picker starts is
yours; the picker runs in the login shell, which is inside no session.

That rule is about creating. `sp restore-exact` and `sp restore` bring back a
conversation that already had an owner, so they carry the origin it was closed
with, whichever way it goes and wherever you run them, and a conversation with
no record is yours. See
[picker-navigation](picker-navigation.md#machine-sessions) for the full rule,
including the one case where an unstamped session is listed as a machine's.

Sessions use modern Bash on macOS even when the account's normal SSH
shell is zsh.

A new Claude session starts with an explicit UUID, so native macOS process
evidence can bind it immediately. A new Codex TUI has no conversation UUID until
its first message; before then the picker labels it `Codex started, no messages
yet`. Opening and closing that terminal work normally, while recovery, fork,
name, and color actions wait for Codex to publish its conversation identity,
because each depends on that UUID. With the optional Codex App Server integration
active, Session Kit also accepts an exact conversation rollout held open by the
managed app-server process, and refuses the association when that server has
zero or multiple candidate conversations.

## Accounts

```text
sp account list
sp account enroll <claude|codex> <alias> <email>
sp account verify <claude|codex> <alias>
sp new <claude|codex> [project-alias] --account <alias>
```

Enrollment creates a separate provider profile. Claude Code profiles use
separate `CLAUDE_CONFIG_DIR` roots and Codex profiles use separate `CODEX_HOME`
roots. Complete the provider's normal sign-in, then run `verify`. Session Kit
does not ask for, copy, or show the provider token.

The home screen shows a short account alias in an aligned column beside `CLD` or
`CDX`. Session detail shows the bound email and provider-supplied plan, and `sp
account list` includes the last verification time. The account-choice screen
shows current Matrix health, usage, and any provider-qualified recommendation.
Missing details remain `unknown`; Session Kit does not estimate them.

To change the account for an existing Claude or Codex conversation, open the
session's actions in the picker and choose `Change account`. It does not take
over a session that is open in another window. Choose a verified account; the
switch runs on Enter and the line says what changed. Nothing is asked first,
the evidence below is taken before the switch, not after a question.

Session Kit proceeds only when fresh evidence proves the exact conversation has
no active turn, tool, hook, subagent, child agent, or background provider work. A
provider waiting for your reply is idle enough to switch. Session Kit transfers
only the exact conversation artifacts, resumes the same provider UUID under the
selected profile, and verifies it before changing the displayed alias. The
conversation history, title, project, color, and session number stay the same.
If the target resume or account check fails, Session Kit restores the private
checkpoint and original binding, then tries to resume the exact conversation on
the original profile. It reports the exact boundary if rollback cannot be
proved.

A session created before account profiles existed needs a one-time managed-shell
recreation for its first account change. Session Kit explains this, then proves
the conversation has no active work before it starts. The original window
briefly reconnects, but the exact
conversation UUID, history, title, project, color, and session number remain.
Later changes use the account-aware shell and do not repeat that migration.

One account change is automatic. When the usage feed shows the account a live
conversation is signed in to has no weekly quota left, Session Kit carries that
conversation to another enrolled account and tells you afterwards. It happens
at most once per conversation: a session that spends a second account stops and
reports rather than walking to a third. It never moves into an account with
less than a quarter of its weekly window left, and it never moves at all while
the usage feed is stale or unreadable. If nothing qualifies, it stands down and
waits for you.

Four things it will never do on its own: switch an account back on that you
switched off, move a session whose live account disagrees with its record,
silently drop a model you asked for by name (the handoff carries the same model
record), or leave a failed move needing recovery, if the handoff does not
prove itself, the conversation stays where it was.

If a message about a move cannot be delivered, it stays owed and is retried
rather than being marked as sent.

`sp account-auto-switch <session>` says what would happen to one session and
changes nothing. Add `--apply` to actually move it. The periodic check is part
of the watchdog, which passes `--apply` itself; that only happens when the
watchdog is in `SESSION_KIT_WATCHDOG_MODE=repair`, the same setting that lets
it act on a live session at all. Only one pass at a time may run on a machine.

When the
owner-only `$XDG_STATE_HOME/session-kit/account-switching-off` sentinel exists,
Session Kit disables enrollment, new account selection, manual switching, and
automatic switching, without deleting profiles, changing provider-owned login
data, or stopping running providers. Its default path is
`~/.local/state/session-kit/account-switching-off`. Nothing in Session Kit
creates that file for you.

## Find and inspect

```text
sp list
sp detail <session-number|shpool-id>
sp find <text>
sp health
```

`sp detail` also lists each live child shell or worker that has been running
for at least one hour. Its age appears on the same row. This reuses the normal
session process inventory; a recorded child conversation with no exact live
process is not given an estimated age.

Sessions outside the kit appear in a separate read-only view. They have no
number and cannot be opened here.

## Open or move

```text
sp go <session-number|shpool-id>
sp takeover <session-number|shpool-id>
```

`sp go` opens a detached exact managed session. `sp takeover` moves an attached
session to the current terminal after a fresh identity check; the earlier window
returns to its picker, and the provider conversation is not duplicated.

## Names

```text
sp name <session-number|shpool-id> <title>
sp name reset <session-number|shpool-id>
```

Title priority is:

1. local manual name;
2. explicit provider rename;
3. retained automatic title;
4. deterministic provider and project description;
5. shortened identity fallback;
6. shell command or `Idle shell`.

Reset removes only the local manual name. It does not hand the session back to
automatic naming: once you have named a session, nothing renames it for you.

Renaming inside the provider counts too. `/rename` in Codex or Claude is read
back, becomes the session's name everywhere Session Kit shows it, and is kept
as a name you chose, so no automatic pass restores what it replaced. It also
wins over an earlier `sp name`: Session Kit knows the last title it pushed into
the provider itself, so a provider title that differs from it was typed after
that push, and the newer of your two names is the one you meant. Your capitals
are yours; a name is stored and shown exactly as you typed it. A session renamed
this way before you upgraded settles itself the first time the kit reconciles;
you do not have to rename anything twice.

## Display setup

### Recommended terminal: Ghostty

Session Kit is developed and tested against Ghostty. Stock Ghostty already
supports the title pushes and truecolour sequences the kit uses, so it needs no
display-related setting. Keep your own font, theme, shell-integration, and
window settings.

Any truecolour terminal can run Session Kit. The key-driven picker writes
24-bit colour and uses synchronized terminal output during repaint. The
cursor-driven picker falls back to a smaller palette when the terminal reports
fewer than 256 colours. Set `NO_COLOR=1` or `SESSION_KIT_NO_COLOR=1` for plain
text; state words and labels keep the same meaning.

### Claude Code status line

Installation places `config/claude/statusline.sh` at
`~/.claude/statusline.sh` and registers it in every managed Claude profile with
a two-second refresh interval. Line 1 is:

```text
<session name>, [MODEL] user@host:/directory | N% context
```

The session name uses its kit colour. The inventory lookup for that tint uses
the default `~/.local/state/session-kit/inventory.json`; a custom
`SESSION_KIT_STATE_DIR` or `XDG_STATE_HOME` therefore gets the safe bold-name
fallback. The rest of the line still works.

Line 2 is an extension point:

```text
5h N% <time left> · 7d N% <time left> · account@example.invalid
```

Without a valid cache it reads `quota --` and leaves line 1 intact. An
executable `~/.claude/statusline-quota-refresh.sh` may fill the cache. Session
Kit calls it in the active profile, no more than once every 180 seconds and in
the background. It must write `$HOME/.claude/cache/quota_headers` as `key:
value` lines containing:

- `x-probe-account`, matching the profile email;
- `anthropic-ratelimit-unified-5h-utilization` and
  `anthropic-ratelimit-unified-5h-reset`;
- optionally the matching `7d` utilization and reset fields.

The installed example is
`~/.local/lib/session-kit/current/extras/statusline-quota-refresh.example`.
It deliberately leaves the vendor endpoint as a TODO and makes no network call
until you supply one. Copy it beside the status line only after filling that
TODO; the core kit never handles credentials or makes this request for you.

The status-line quota segment uses Linux utilities, including GNU `stat`; the
name, title, and colour integrations do not depend on it.

### Claude attention evidence

The optional `attention_hook.sh` makes `needs you` react to Claude events
between polls. It is hand-deployed: install, update, rollback, and uninstall do
not copy, register, or remove it. Register the installed canonical copy for
Claude's `Notification`, `UserPromptSubmit`, and `SessionEnd` events, as shown
in [the Claude integration contract](../config/claude/README.md#how-the-kit-learns-a-session-is-waiting-for-you).
`SESSION_KIT_ATTENTION_SOURCE=auto` merges hook and polling evidence; `poll`
ignores hook records, and `hook` is intended for drills.

### Codex status bar

Session Kit does not configure or replace the Codex status bar. Codex owns that
surface. For each managed launch, the kit passes two command-line overrides:
the installed `tui.terminal_title` items (`activity` and `thread` by default)
and the session's `sk-<colour>` theme. These options apply to that process only
and never edit `~/.codex/config.toml`.

A new Codex process can start before the conversation has a name, so its own
bar may temporarily show an identifier. Opening a detached, proven-idle
provider can refresh it automatically. For an attached provider, the
key-driven picker offers the guarded refresh action only when the exact process
is idle, has no subagents, and has a stored title. That maintenance action is
not exposed by the cursor-driven action panel. An App Server refresh restarts
only the generation-bound remote TUI, not the server or conversation.

### Terminal window and tab titles

`sp go`, `sp takeover`, `sp new`, and picker open or move push the session name
with the standard OSC 0 terminal-title sequence. The kit strips control bytes,
collapses whitespace, and caps the title at 64 characters. Returning to the
picker restores `session kit`. A restore does not claim the tab before the
session is actually opened.

Claude may update its own title at SessionStart or after the next submitted
prompt. Codex receives `activity` and `thread` as per-launch title items. Set
`SESSION_KIT_TAB_TITLE=off` to disable both the kit's title pushes and the Codex
override; Session Kit deliberately does not set Claude's vendor title-disable
switch because that also suppresses hook-provided titles.

### Session colours

Every Claude Code and Codex session carries an identity-derived colour on its
picker row and in provider chrome. Shell sessions have none. Collisions walk
the provider's palette until every colour is in use:

- Claude: red, blue, green, yellow, purple, orange, pink, cyan;
- Codex: lime, magenta, silver, sand, sky, sea.

The palettes are separate, so a Claude and Codex session never share a kit
colour. Codex receives an installed theme with a per-launch override. Claude
receives name and colour transcript records before its first visible frame;
an explicit provider `/rename` or `/color` remains authoritative.

To settle existing live assignments after a palette or authority change:

```bash
sp color reconcile
```

The command moves only sessions that need to move and is safe to repeat. A
running Claude window applies a changed provider colour at its next start or
resume. Picker semantics never depend on colour alone.

## When a provider exits

A clean exit closes the session. `/exit` inside Claude or Codex, the provider
ending with status 0, ends the shell with it, returns the session number to
the ordinary quarantine, and lands you at the picker. The conversation is in
Closed sessions, and one Restore brings it back. There is no opt-in marker: one
policy, on every machine.

A crash is different. On a non-zero status the conversation reopens itself
once, with a one-line notice saying so. A second crash within a minute stops
the loop, so a conversation that cannot start cannot spin. What happens then
depends on one question, and the session is told which way it went:

- if the conversation can be brought back, the session closes and lands in
  Closed sessions under its own name, exactly as a clean exit does;
- if it cannot, no conversation was ever recorded, its transcript is gone or
  unreadable, the Closed list could not be written, or you had marked the
  session `keep`, the session STAYS OPEN and says which of those it was.
  Closing it would be the one move that loses the conversation for good, so
  the kit leaves it for you to close from the picker.

A reopen that is REFUSED rather than run never closes the session, whatever the
reason: a refusal is not evidence that a conversation is finished, and one of
the reasons is that a provider is still running in that terminal.

Nothing asks you to choose.

To leave a conversation RUNNING and walk away, press **Ctrl-q**. It detaches,
the provider keeps working, and the session's number brings you back to the
same live conversation later, the same outcome as closing the terminal window
without typing anything, but without having to close a window to get it.
Ctrl-q works in every session, whichever provider is running, because the
session manager handles it before the provider ever sees the key.

Session Kit puts that binding in the shpool config it writes for a fresh
setup. It is not shpool's own default, stock shpool detaches on the two-key
chord **Ctrl-Space Ctrl-q**, so if you already had a shpool config before
installing Session Kit, yours was left alone and Ctrl-q may do nothing there.
Add this to `~/.config/shpool/config.toml`. shpool compiles keybindings once
per attachment, so it needs no daemon restart, but it reaches only windows you
open or REOPEN after the edit. A window you are already sitting in keeps the
binding it started with, which is why Ctrl-q can work in one of your terminals
and do nothing in the one next to it:

```toml
[[keybinding]]
binding = "Ctrl-q"
action = "detach"
```

**In Claude only**, `/kit` does the same thing from inside the conversation.
There is no Codex equivalent, and the two are not symmetric: Ctrl-q is the one
way that works everywhere, and it is the one to learn.

Why there is no `/kit` in Codex: in codex-cli 0.145.0 the slash namespace is
built-in commands only. Its app-server protocol has no channel for a custom
command, `~/.codex/prompts/` is not read at all, and a file installed as a
skill is discovered but never appears under `/`. An older Codex did honour
`~/.codex/prompts/`, which is why this page used to say so. If a future Codex
brings custom slash commands back, measure it before believing it,
`tests/test_install.py` pins what was measured.

If you installed an earlier release, `~/.codex/prompts/kit.md` is still on
disk. Nothing reads it and nothing runs it, so it is clutter rather than a
risk, and the installer deliberately does not delete it: an upgrade that
removes files from a provider's own home is a worse habit than an inert
leftover. Delete it yourself whenever you like.

The kit installs the Claude verb into `~/.claude/commands/kit.md` AND into
every enrolled Claude account profile under
`~/.local/share/session-kit/accounts/`, because a session launched on an
account reads that account's commands and nothing else. It is kept with the
release. It runs one line, `shpool detach`, which detaches the session named
in `$SHPOOL_SESSION_NAME`. A provider that is not installed on the machine gets
nothing, an existing `kit.md` that is not a plain file is left alone, and the
account profile a session happens to be using is never written to.

Only a typed `/kit` ever detaches. Claude Code treats a file in `commands/` as
a skill it may invoke on its own when the conversation looks relevant, so the
verb carries `disable-model-invocation: true`: the description is not placed in
the model's context at all, and nothing but the typed command can fire it.
Without that, typing the bare word `kit` as an ordinary message was enough to
detach a terminal mid-turn.

Do not use a generic latest-conversation command to come back. If exact
identity cannot be proved, reopen is disabled rather than guessed.

From an ordinary managed shell, `keep_session` disables automatic cleanup,
`unkeep_session` allows it to resume once all safety conditions are met, and
`bye` closes the session immediately, no question, and recoverable from Closed
sessions like any other close. A directly typed Bash `exit` keeps its normal
shell behavior.

## Close a session

In the picker, type the numbers you want, press Enter, and choose Close. From a
shell:

```text
sp close <session-number|shpool-id>
```

Before closing, Session Kit refreshes live evidence and resolves every number to
one exact shpool ID. Cached inventory, changed generations, hidden numbers, or
ambiguous identity disables the whole action rather than part of it. Nothing is
confirmed first, and the line afterwards says what happened: `Closed 3
sessions.`

Every deliberate close is written to a ledger, provider, conversation, title,
directory, and the time, and the conversation is listed under Closed sessions
whenever it is not already open. The retained row is hidden while the exact
conversation is live and reappears if that live session is later lost. That is
what makes a confirmation unnecessary: a close you did not mean is one Restore
away.

Closing ends the session. It does not delete provider-native conversation
history or optional journal files.

## Change the model

```text
sp change-model <session-number|shpool-id> <model-id>
```

The exact conversation comes back on the model you named, through the same
launch path `sp new --model` uses. In the picker it is the `Change model`
action, which offers the models you have configured: one `provider<TAB>model`
line each in `~/.config/session-kit/models.tsv`. Session Kit ships no model
catalog and never guesses a model name. The picker shows only identifiers the
same provider-specific launch gate accepts (`claude-...` for Claude; `gpt-...`,
`o3...`, `o4...`, or `codex-...` for Codex), so every offered choice can reach
the model-change safety checks.

### The model you chose is the model you get

**Session Kit never changes a model by itself, and says so before a session
starts rather than after it has run on something else.** Both `sp new --model`
and `sp change-model` ask one more question before anything launches: does this
machine really serve that model? Two local answers exist, and the first that
applies wins:

1. **What was served last time.** When something reads the model a session is
   really on, it records the pair with `session-kit`'s `model-served` verb. A
   recorded answer that differs from the request is a silent downgrade, and the
   next launch refuses it by name.
2. **Your `models.tsv`.** A model that is not on this machine's list is
   refused, and the refusal prints the list.

With neither, the answer is `unknown`, said as unknown, never as verified, and
the session starts exactly as asked.

A refusal names the model you asked for and what would serve it instead, and
nothing starts. Nothing is ever substituted: `--model <what-serves-it>` takes
the other model, and `--model-anyway` repeats the request as it was.

```text
sp new claude myproject --model claude-fable-5
You asked for claude-fable-5.
The last session that asked for it actually ran on claude-haiku-4-5 instead.
Start it on claude-haiku-4-5 with --model claude-haiku-4-5, or repeat the
command with --model-anyway to ask for claude-fable-5 anyway.
session-kit: no session was created
```

A session that is already running is never restarted by this check, it applies
to a launch and to a model change, not to recovery.

## Isolated workers

**Delegated work gets its own copy of the code.** A machine session, one
started with `--origin machine`, or with `SESSION_KIT_ORIGIN=machine` in the
environment, is given a git worktree of its own without asking, on a branch
the kit cuts for it, so two workers on one repository never edit the same
files. A directory that is not a git repository has nothing to isolate: the
session runs in it directly and says so. An automation that means the checkout
itself says `--no-worktree`.

A person's own session is never moved out of the directory they chose. A
worktree is theirs to ask for, by branch name:

```text
sp new claude --worktree <branch>
sp new codex myproject --worktree <branch>
```

The branch is materialized as a worktree under the state directory
(`worktrees/trees/<project>-<8 hex>/<branch>`, with any `/` in the branch name
written as `-`; the hex tells two projects of the same name apart, and
`sp worktree list` prints the exact path). The session starts there. The call
is idempotent per repository and branch: starting a second session on the same
branch reuses the same directory rather than forking a second one. A branch
already checked out somewhere else, or a project that is not a git repository,
refuses the launch, no session is created.

Rows for isolated sessions carry their branch in the picker and in `sp list`,
and `sp detail` shows it as `Worktree`.

```text
sp worktree list
sp worktree materialize --repo <directory> --branch <branch>
sp worktree teardown --branch <branch> [--merged-into <ref>] [--force]
sp worktree sweep [--active <shpool-id>]... | --none-alive | --active-stdin
```

**Closing the session gives the copy back.** Every close does it, `sp close`,
the picker's `k`, `bye` or a clean provider exit, and the reaper's scheduled
pass, so nobody has to remember a teardown.

**`git status` is not the question.** Before a copy goes, everything work can
be is checked, and any one of these keeps it, with the reason said out loud:

- uncommitted, staged or untracked files;
- **files the repository ignores**, a log, a screenshot, a report, a `.env`, a
  one-off script. A fresh copy starts with none, so every one of them was made
  by whoever worked there;
- the commit the copy is actually standing on not being in the reference
  (`HEAD` by default), its **real** HEAD, so a commit made on a detached HEAD,
  which lives nowhere but that copy, is never removed with it;
- a stash made from it;
- a merge, rebase, cherry-pick, revert or bisect left half-finished;
- a submodule with anything of its own in it;
- anybody still working in the directory, named by pid;
- the checkout there not being the one recorded, a directory made by hand
  where a copy used to be is somebody else's;
- a copy the kit did not cut for that session: a worktree asked for by branch
  name is the operator's, and so is every worktree that was on the machine
  before this shipped. Those are removed by `sp teardown` and nothing else;
- **a question git could not answer.** Every check above exists to prove the
  work is safe, so one that could not run has not passed. An unreadable HEAD,
  a `git stash list` that failed, a repository that has gone missing, a
  comparison that errored, a process table that could not be read: each is
  named and each keeps the copy.

`--force` covers uncommitted, staged, untracked and ignored files, a stash, a
submodule, an unmerged branch, and a check that could not run. It does **not**
cover:

- a commit on no branch that is not *proved* to be in the reference, the copy's
  own reflog is the only thing that reaches it, and removing the directory takes
  that reflog with it;
- a half-finished merge, rebase, cherry-pick, revert or bisect, the replay
  state lives only in the copy. Finish it or undo it there first; the refusal
  names the command;
- a checkout that is not the recorded one;
- a repository the kit may not write into (below);
- a live session standing in the directory.

A copy kept because a worker was still writing is not kept forever: the reaper
runs the sweep against the live session list on **every** scheduled pass,
including the ones with no session to close, which are most of them. Run it
yourself with `sp worktree sweep --active <shpool-id>…`; with no `--active` and
no `--none-alive` it refuses, because a sweep that is told nothing must never
read that as "nothing is alive". `--active-stdin` is the machine form: it reads
a `shpool list --json` payload and refuses outright on anything it cannot fully
account for, so a reader that broke can never look like an idle machine. A copy
whose session was never recorded is reported and left alone however old it is,
age is not ownership.

**A checkout that is the running thing is never copied.** Some directories are
served live, and a copy of one is work that never takes effect, the session
edits the copy, the site keeps serving the original, and the check that says
"done" reads the old page.

Three ways a repository is recognised as one of those, and the first needs
nobody to have configured anything:

- **it is under a system root**, `/srv`, `/var`, `/opt`, `/usr`, `/etc`,
  `/root`, `/run`, `/bin`, `/sbin`, `/lib*`, `/boot`, `/dev`, `/proc`, `/sys`,
  or `/` itself. That is where a machine keeps the software it runs. This is on
  by default, deliberately: a guarantee that has to be switched on is not one;
- it carries a `.session-kit-shared` file at its root;
- the host lists it in `SESSION_KIT_SHARED_REPOS` (colon-separated absolute
  paths).

`SESSION_KIT_COPYABLE_REPOS` (same format) is the way to say a particular
directory *is* yours to copy, and it overrules all three.

A delegated session in one of these runs in the checkout itself and says why.
Nothing is written into such a repository, no branch, no worktree
administrative directory, no removal, and no session ever edits a copy of it by
accident. The same rule applies to a linked worktree of one: a worktree
resolves to the repository it belongs to, so it is covered by whatever covers
that repository.

**No repository-wide prune, ever.** Giving a copy back unregisters exactly the
one entry that names that directory. `git worktree prune` is repository-wide
and drops the administrative directory, index, HEAD, reflog, half-finished
rebase, of *every* worktree of that repository it cannot reach at that moment,
including one on a volume that simply is not mounted right now.

When you want the close and the prune in one verb:

```text
sp teardown <session-number|shpool-id> [--merged-into <ref>] [--force]
```

`--force` removes a directory that is dirty or unmerged anyway. It does not
override a live session standing in it, and neither form ever deletes the
branch, the commits outlive the worktree.

## Automatic cleanup

This is a BACKSTOP, not the ordinary route. A session whose provider exits
closes itself at once (see "When a provider exits" above), so a terminal that
reaches the 72-hour window is one the shell could not close: a session started
before this release landed, a close that failed, or a session the manager still
lists after its shell is gone. Two shapes are deliberately outside it and stay
visible until you close them from the picker, a terminal with no provider-exit
record, and one with no exact conversation, because a row whose state cannot
be proven is never cleaned up quietly.

The scheduled observer considers only disconnected provider-exited terminals,
and automatic close begins only after the cleanup timer is enabled. A candidate
then needs 72 continuous hours with every exact safety predicate unchanged.

Attachment, a live provider, child work, a recovery conflict,
an identity change, missing evidence, or a new terminal generation resets or
blocks eligibility.

`sp prune` runs fresh checks for a manual cleanup.

## History and recovery

```text
sp history <session-number|shpool-id>
sp find <text>
sp recover
sp recover --allow-large-ledger
sp restore --allow-large-ledger <number>
```

History is the readable record of a session, and it is an action on every row
in the picker. Where a recording exists it is replayed as settled text in a
pager; a session with no recording, recording is off unless you turned it on,
and a session started without it can never be recorded after the fact, falls
back to the conversation transcript and says so on the line above the text.
Claude and Codex transcripts use the same layout for operator messages, model
messages, tool calls, and tool output.

`sp find` searches stored recordings case-insensitively. Each recording keeps
its own result group and example-line allowance. A current or recoverable
session uses its recorded name; older recordings without trustworthy title
metadata use their start date and first readable request instead. Storage paths
and internal session identifiers are never printed. When recordings have the
same display name, a recording ordinal keeps their result headers distinct.
With no result it prints
`Matches: none.` A damaged recording is skipped with a one-line notice while
the rest of the search continues.

Clean recordings are prepared incrementally in the background. If the clean
copy trails its capture by more than the two-second live-write grace period, or
an on-demand refresh fails, `sp history`
says that the last readable version is being shown. If no clean version can be
produced, it says that the recording as captured is being shown. These are
successful recall notices, not errors; redirected stdout still contains only
the recalled text, with the notice on stderr.

Restoring a closed conversation is the Closed sessions row in the picker, or
`sp restore` at the command line. Both read one list, so they show the same
conversations in the same order under the same names, and a row is acted on by
what that row shows: its session number, the one it has on every other screen
, or, when its number has been retired, the name beside it. Two rows that
answer to one name (everything nobody named reads "unnamed") each show the
time of their own event where the number would be, and that is what you type.
A word belongs to one row on the whole list or to none of it: a conversation
somebody named `2` does not answer to `2` while session 2 exists, and shows
the time of its own event instead.

A name and an event time belong to the list rather than to the conversation,
so they do move to another row when the list is rebuilt, that is what they
are for, while a name is shared. What cannot happen is one moving between the
screen you read and the restore you type. Each screen writes down what it
printed beside each row; `sp restore` checks the word you hand it against that
and refuses in a sentence, restoring nothing, if it now means a different
conversation. The picker draws and acts out of one list, so a word typed there
always answers for the row in front of you. If a screen cannot be written
down it says so, because the check is missing rather than passing.

A restore still resolves an exact Claude or Codex conversation identity and
never falls back to recency, directory, or "latest". One conversation cannot be
open in two writable sessions at once: open the session that has it, or fork it
deliberately. A live conversation is never offered. A shell session cannot be
resumed at all, its history is all that remains of it, and neither can a
conversation whose transcript this machine can no longer read; both are listed,
and both say so in a sentence rather than offering a restore that cannot
work.

Normal refreshes stream and completely validate the durable Closed sessions
ledger. A pathological file above the normal safety ceiling is never shown as
an empty or partial list: recovery exits non-zero, names the exact size and
ceiling, and prints the command above. The explicit large-ledger form applies
the same complete row validation without the unattended-refresh ceiling; its
footer gives the matching restore command for the numbers it prints.

## Repair

```text
sp repair <session-number|shpool-id>
```

Repair is reserved for direct evidence of an unrecoverable shpool handoff
failure. Silence alone does not qualify.

The installed watchdog reports evidence and does not repair in its default mode.
Its advanced repair mode is a separate Linux-only opt-in that carries
terminal-state risk. The macOS watchdog is report-only.

If *every* session becomes unreachable at once rather than one, that is a
different problem and repair will not help. See
[Troubleshoot](troubleshooting.md).

## Automation

Noninteractive close requires an explicit opt-in and the exact shpool ID:

```bash
SESSION_KIT_NONINTERACTIVE=1 \
SESSION_KIT_CONFIRM_ID=<exact-shpool-id> \
sp close <exact-shpool-id>
```

Never automate moves, kills, repairs, or cleanup using display numbers. They are
stable within a boot, but they describe what is on screen, not which
conversation you mean.
