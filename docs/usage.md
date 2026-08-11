# Use Session Kit

## The picker

With shell integration enabled, SSH opens an ordinary shell. Type `kit` to open
the picker. Linux Bash may print a short `kit: open sessions` hint. On macOS,
zsh receives the command path but does not start the picker or replace the
shell.

```text
number          open a ready session or inspect one open elsewhere
n               guided new Claude Code, Codex, or shell session
k <numbers>     close displayed sessions: k 5 · k 5, 6, 8 · k 4-7 · k all
x <numbers>     compatibility alias for k
/text           search names, providers, projects, and exact IDs
r               refresh live state and clear search
m               more: outside roots, recovery, and projects
o               show provider roots outside shpool, read-only
u               review exact recovery records
p               add, review, or drop projects (under m)
name <number>   set a local provider title
name reset #    remove the local title
fork <number>   create an independent provider conversation
next / prev     change page
?               show help
Enter           return to a regular terminal
```

Selecting a session that is already open elsewhere shows a move menu. Session
Kit never moves it without a separate, explicit choice. For an account-aware AI
session, the same menu can offer `Change account`; this changes the provider
login for the exact conversation without taking its terminal away from the
other window.

No Session Kit screen prints a shpool ID or a provider UUID — not rows, not
`sp detail`, not a confirmation or an error, and there is no flag that reveals
one. Sessions are named by their terminal number and their title everywhere a
person reads. Identifiers stay in the 0600 state files, the JSON output modes
(`shpool_status --json`, `--lookup`), and proofs. Commands still *accept* an ID
wherever they accept a number, so a script that already holds one keeps
working.

### Why the screen sometimes clears while you type

An idle picker keeps itself current. It collects a snapshot in the background
every few seconds and repaints only when something visible changes, so it never
runs in front of your prompt. Terminal numbers are stable, so a repaint never
changes what a number means, and the active search and page survive one.

A repaint clears the screen. Characters you typed without pressing Enter live in
the terminal's own input queue, which this process cannot inspect: in canonical
mode the kernel reports an incomplete line as no input at all, on both Linux and
macOS. So a repaint landing mid-command erases the *echo* of what you typed
while the characters themselves stay queued and intact. Press Enter and the
command you started still runs.

Set `SESSION_KIT_PICKER_REFRESH_SECONDS=0` to switch this off, or to a number of
seconds (minimum 2) to change the pace.

## Start a session

```text
sp new claude [project-alias]
sp new codex [project-alias]
sp new shell [project-alias]
sp new <project-alias>
sp new claude [project-alias] --account <alias>
sp new codex [project-alias] --account <alias>
```

The last form uses the provider stored for that project alias.

Guided New asks for the provider, project, and account before launch. When Matrix
has current, healthy advice that names the same provider, its recommended account
is preselected and Enter confirms it. Missing, stale, unhealthy, logged-out, or
provider-mismatched advice leaves the account unselected. Codex currently has no
preselection because the available Matrix advice is not provider-qualified for
Codex; choose its account manually. This fail-closed behavior prevents advice
for one provider from selecting an account for the other.

A terminal number stays with the session for the current host boot. It is a
display convenience, not a durable provider identity. Terminal `1` is reserved
exclusively for Fleet Supervisor; ordinary sessions start at `2`, including
when the supervisor is absent.

Managed terminals use modern Bash on macOS even when the account's normal SSH
shell is zsh.

A new Claude session starts with an explicit UUID, so native macOS process
evidence can bind it immediately. A new Codex TUI has no conversation UUID until
its first message; before then the picker labels it `Codex started, no messages
yet`. Opening and closing that terminal work normally, while recovery, fork,
name, and color actions wait for Codex to publish its thread identity, because
each depends on that UUID. With the optional Codex App Server integration
active, Session Kit also accepts an exact thread rollout held open by the
managed app-server process, and refuses the association when that server has
zero or multiple candidate threads.

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

To change the account for an existing Claude Code or Codex conversation from a
second Session Kit window, select its number under `Open elsewhere` and choose
`Change account`. It does not take over the session attached in the other
window. Choose a verified account and confirm the change. Every switch requires
its own confirmation.

Session Kit proceeds only when fresh evidence proves the exact conversation has
no active turn, tool, hook, subagent, child agent, or background provider work. A
provider waiting for your reply is idle enough to switch. Session Kit transfers
only the exact conversation artifacts, resumes the same provider UUID under the
selected profile, and verifies it before changing the displayed alias. The
conversation history, title, project, color, and terminal number stay the same.
If the target resume or account check fails, Session Kit restores the private
checkpoint and original binding, then tries to resume the exact conversation on
the original profile. It reports the exact boundary if rollback cannot be
proved.

A session created before account profiles existed needs a one-time managed-shell
recreation for its first account change. Session Kit explains this and asks for
explicit confirmation. The original window briefly reconnects, but the exact
conversation UUID, history, title, project, color, and terminal number remain.
Later changes use the account-aware shell and do not repeat that migration.

Account changes are always manual. Matrix may recommend an account for a new
session, but Session Kit does not rotate an open thread automatically. When the
owner-only `$XDG_STATE_HOME/session-kit/account-switching-off` sentinel exists,
Session Kit disables enrollment, new account selection, and switching without
deleting profiles, changing provider-owned login data, or stopping running
providers. Its default path is
`~/.local/state/session-kit/account-switching-off`.

## Find and inspect

```text
sp list
sp detail <terminal-number|shpool-id>
sp find <text>
sp health
```

Provider roots outside shpool appear in a separate read-only view. They have no
terminal number and cannot be opened through Session Kit.

## Message your sessions

`sp msg` opens the message centre: write to all, idle, or one session, watch
replies arrive on the same screen, and answer from there. The picker opens it
with `s` and shows `✉ N new replies` while replies wait. The full story — who
is reachable, what recipients experience, cost, and switches — is in
[messaging](messaging.md).

## Open or move

```text
sp go <terminal-number|shpool-id>
sp takeover <terminal-number|shpool-id>
```

`sp go` opens a detached exact managed session. `sp takeover` moves an attached
session to the current terminal after a fresh identity check; the earlier window
returns to its picker, and the provider conversation is not duplicated.

## Names

```text
sp name <terminal-number|shpool-id> <title>
sp name reset <terminal-number|shpool-id>
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
as a name you chose — so no automatic pass restores what it replaced. It also
wins over an earlier `sp name`: Session Kit knows the last title it pushed into
the provider itself, so a provider title that differs from it was typed after
that push, and the newer of your two names is the one you meant. Your capitals
are yours; a name is stored and shown exactly as you typed it. A session renamed
this way before you upgraded settles itself the first time the kit reconciles;
you do not have to rename anything twice.

## New replies

Replies to `sp msg` appear in the picker as rows, not as a count:

```text
  New replies
    r1   #4            Fleet rebuild            claude  12m ago
    r2   not open here  Ledger work             codex   2h 5m ago
```

`r1` opens the report that reply belongs to. A reply outlives its session, so
one from a room that has since closed, or that lives outside the manager, is
still listed and still opens — named by what the send recorded.

Every row with a key opens something. A reply whose message has been pruned is
still shown, because losing it would be worse, but it is shown as `-` rather
than a key it could not honour, and `s` reads it in the message centre. The
section is bounded; when there are more than it shows, it says how many.

## Session colors

Every Claude Code and Codex session carries its own color. It appears on the
picker row and in the session's own window, so a glance tells you which terminal
you are looking at. Shell sessions have no provider color.

A conversation's color is derived from its identity, so it is the same every
time you return to it. If another live session of the same provider already
holds that color, the newcomer takes the next free one instead, which is what
keeps two rows on screen from looking alike. When every color in a provider's
palette is already in use, colors begin to repeat.

The two providers draw from separate palettes, so a Claude session and a Codex
session can never share a color:

- Claude sessions use red, blue, green, yellow, purple, orange, pink, and cyan.
  That set is fixed by Claude Code itself, which accepts those eight names and
  no others.
- Codex sessions use lime, magenta, silver, sand, sky, and sea. Codex reads a
  theme file from disk and applies no allow-list, so Session Kit ships one theme
  per color.

Sessions that were already running when a palette changed keep whatever they
had until each one next starts. To settle all of them at once instead:

```bash
sp color reconcile
```

It gives every live session of a provider its own color, moving only the ones
that have to move, and prints what changed. Running it again changes nothing,
so it is safe to repeat. A Claude session shows a color it was moved to from
its next start or resume.

This is separate from the semantic coloring of the dashboard, where color marks
provider, availability, attention, danger, and secondary status. Text labels
always carry the same meaning, so nothing depends on color being available.

## Pending titles

Two providers cannot repaint their own title from outside their process, so
Session Kit marks the row `title pending` rather than showing something untrue.

**Codex status bar.** A new Codex provider may start before its thread name
exists, and it keeps the initial conversation ID in its status bar until that
provider restarts. Opening a detached session can refresh a proven-idle provider
automatically. An attached provider is never restarted automatically: open its
action menu and choose the pending-title action, which is refused unless the
exact provider generation is idle, has no subagents, has a real stored title,
and still matches a fresh proof. The shell and the conversation stay the same.
For App Server sessions the refresh restarts only the generation-bound remote
TUI, leaving the server, Unix socket, exact thread, and managed shell running.

Codex saves a renamed thread immediately, so this maintenance state stays out of
normal dashboard rows: the stored title is already correct. Completed Codex
subagent threads remain in detail output but are not counted as active subagents
in summary rows.

**Claude prompt box.** Claude stores its conversation auto-title separately from
the name and color shown in the prompt box. Session Kit fills absent native name
and color records before each human-facing inventory, and never replaces an
explicit `/rename` or `/color` choice. Claude does not repaint a running prompt
box after another process updates its records, so the dashboard keeps `title
pending` on that live generation. The stored title and color appear when the
exact conversation next starts, including the normal `r` reopen path after
Claude exits. New Claude sessions bootstrap their stable color before the first
visible frame.

## When a provider exits

When Claude Code or Codex exits — whether you typed `/exit` or the provider
crashed — the managed shpool terminal stays alive, the dashboard labels it as
provider exited, and the terminal stops at this menu:

```text
r  reopen the exact conversation
k  keep the terminal and disable automatic cleanup
s  open an ordinary shell and permanently exclude the terminal from cleanup
c  close the terminal
```

Press `c` to close the terminal and return to the picker. The terminal is kept
by default because it is the only thing that still knows the exact conversation
identity `r` needs; closing it is not undoable.

Touch `~/.sk_autoclose_on_clean_exit` to close the terminal immediately on a
clean (zero status) exit instead of showing the menu. A non-zero exit is a
crash and always stops at the menu, whatever that marker says.

Do not use a generic latest-conversation command to come back. If exact identity
cannot be proved, reopen is disabled rather than guessed.

From an ordinary managed shell, `keep_session` disables automatic cleanup,
`unkeep_session` allows it to resume once all safety conditions are met, and
`bye` closes the terminal after an exact-ID check. A directly typed Bash `exit`
keeps its normal shell behavior.

## Close a session

From the picker, `k <number>`. From a shell:

```text
sp close <terminal-number|shpool-id>
```

Before closing, Session Kit refreshes live evidence and resolves every number to
one exact shpool ID. Cached inventory, changed generations, hidden numbers, or
ambiguous identity disables the whole action rather than part of it. Once that
proof succeeds, no per-session confirmation prompt is required.

Closing ends the managed terminal. It does not delete provider-native
conversation history or optional journal files.

## Automatic cleanup

The scheduled observer considers only disconnected provider-exited terminals,
and automatic close begins only after the cleanup timer is enabled. A candidate
then needs 72 continuous hours with every exact safety predicate unchanged.

Attachment, a live provider, child work, a pending reply, a recovery conflict,
an identity change, missing evidence, or a new terminal generation resets or
blocks eligibility.

`sp prune` runs fresh checks for a manual cleanup.

## History and recovery

```text
sp history <terminal-number|shpool-id>
sp recover
```

History requires journals to have been explicitly enabled for that session.

Recovery uses an exact Claude Code or Codex UUID and never falls back to
recency, directory, or "latest". An active UUID cannot be resumed into a second
writable terminal: open the existing terminal, or create an explicit fork.

## Repair

```text
sp repair <terminal-number|shpool-id>
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
