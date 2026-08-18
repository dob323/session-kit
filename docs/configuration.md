# Configuration

Session Kit keeps host configuration outside immutable release directories and
uses XDG locations where available.

## Default paths

| Purpose | Default |
| --- | --- |
| Inventory configuration | `${XDG_CONFIG_HOME:-$HOME/.config}/session-kit/inventory.json` |
| Project aliases | `${XDG_CONFIG_HOME:-$HOME/.config}/session-kit/projects.tsv` |
| Session Kit state | `${XDG_STATE_HOME:-$HOME/.local/state}/session-kit` |
| Optional session history | `${XDG_STATE_HOME:-$HOME/.local/state}/shpool-journal` |
| Recovery journal archive | `${XDG_STATE_HOME:-$HOME/.local/state}/shpool-journal-recovery` |
| Journal archive read by `sp find` | `${XDG_STATE_HOME:-$HOME/.local/state}/shpool-archive` |
| Paired launch records | `${XDG_STATE_HOME:-$HOME/.local/state}/shpool-start` |
| shpool configuration | `${XDG_CONFIG_HOME:-$HOME/.config}/shpool/config.toml` |
| Immutable releases | `$HOME/.local/lib/session-kit/releases` |
| Stable commands | `$HOME/.local/bin` |
| macOS launchd templates | `${XDG_CONFIG_HOME:-$HOME/.config}/session-kit/launchd` |
| Active macOS LaunchAgents | `$HOME/Library/LaunchAgents` |

Private directories should be mode `0700`; private files should be mode `0600`.
Sensitive proof files also require the expected owner, regular-file type, and
link count. Provider transcripts are the one exception, because the provider —
not Session Kit — creates them under your umask: a Codex rollout is accepted
when this uid owns it and no group or other write bit is set. See
[Security and data](security-and-data.md).

## Account profiles

An account profile is a local alias for one Claude Code or Codex subscription
login. It is not a Session Kit cloud account. Claude profiles launch with a
profile-specific `CLAUDE_CONFIG_DIR`; Codex profiles launch with a
profile-specific `CODEX_HOME`. These roots keep each provider's native login and
local conversation data separate.

```text
sp account list
sp account enroll <claude|codex> <alias> <email>
sp account verify <claude|codex> <alias>
sp account configure-feeds <absolute-roster-json> <absolute-advice-json>
```

`enroll` creates the owner-private profile location and registers the alias and
email description. Sign in through the provider's normal interactive login.
Session Kit does not copy, link, import, print, or log credentials from another
profile. `verify` then checks that the provider's reported signed-in identity
matches the registered description. An unverified or logged-out profile is not
eligible for a recommendation or account change.

Aliases are short labels intended for the home screen. `sp account list` keeps
the registered identity and last verification time. Session detail shows the
bound alias, email, and provider-supplied plan. The account-choice screen shows
current roster health, usage, and any provider-qualified recommendation.
Provider data that is missing or cannot be verified is shown as unknown rather
than inferred.

The default Claude and Codex homes remain usable as legacy profiles, but Session
Kit does not invent an email identity for them. Enroll and verify a named profile
before relying on account-aware recommendations or switching.

One account change happens without being asked: a conversation whose own
account has run dry is carried to another enrolled account, once, and the
change is reported afterwards. Every condition below has to hold, and each is
a number from the usage feed rather than an estimate:

* the roster and the rotation advice are both fresh. A stale or unreadable
  feed disables automatic switching outright, because stale advice is how a
  machine drains an account it believed was full;
* the conversation's own account is spent — its published weekly usage has
  reached `SESSION_KIT_ACCOUNT_EXHAUSTED_PERCENT` (default `100`). A spent
  five-hour window alone does not qualify: it refills on its own within five
  hours, and moving for it would spend the single move for nothing;
* the account it moves to still holds a reserve —
  `SESSION_KIT_ACCOUNT_RESERVE_PERCENT` (default `25`) of its weekly window
  must be unused. An account past that line is refused even when it is the
  only candidate left. An account whose published numbers are missing, or are
  present but not readable as window fractions, is never a candidate: unknown
  is never treated as "no objection" in either window;
* the conversation has not already been moved automatically. A session that
  spends a second account stops, reports, and waits. It never walks to a third.
  A move is counted from the moment it is reserved, before anything
  irreversible happens, so a failure part-way through reads as "already moved"
  rather than as no move at all; only a proven return to the original account
  gives it back;
* the live session agrees with the record. The account a conversation is
  actually signed in to is published per row, and a row that disagrees with
  the account on file, or that carries no readable account, is left alone —
  the account found dry has to be the account the process is really using;
* an explicitly requested model is still generation-bound to the same managed
  shell. The handoff rebuilds that model record and resumes with the same
  `--model`; a missing or mismatched record refuses the relaunch rather than
  silently selecting the provider default.

Anything else is a hold with a stated reason, and a hold changes nothing. When
no account clears the reserve, automatic switching stands down and the work
waits for the owner rather than draining what is left. Usage is published per
account, never per conversation, so no rule here depends on knowing what a
single conversation spent.

Nothing automatic ever enables an account. Re-verifying a target profile
re-reads it under the state lock and refuses if it was switched off while the
provider was being asked to identify itself, so an account the owner disables
stays disabled even when that change happens in the middle of a handoff.

A failed handoff leaves the conversation where it was. The unattended path
rolls back without the picker's destructive last resort — that path closes the
session and re-creates it, which is not "still working" — and where even the
rollback cannot be proven it changes nothing further and says so.

A number the feed publishes that cannot be a window fraction condemns the whole
feed, not just its own row. A single value cannot reveal a units change — if
percents replaced fractions, `55` is obviously wrong but `0`, `1` and `2` still
parse — so one impossible number means none of that feed's numbers are judged.

### Before you turn automatic switching on

Read this first, because the consequence lands on your account rather than on
this tool.

Holding several subscriptions and doing your own work on each one is ordinary
use. Nothing here shares an account between people, resells access, or puts a
subscription token into a harness of its own: every session launches the
provider's own binary against its own configuration directory, and the kit
never reads, copies, prints, or logs a token. That is the side of the line
providers have drawn.

Moving a conversation to another account *because the first account hit its
limit* is a different shape. However many subscriptions you paid for, an
automated move triggered by exhaustion is the pattern that limit-evasion
enforcement is built to find, and enforcement acts on accounts. This is a
judgement about your own risk, and it is why the mechanism ships off, why the
installer never enables it, and why nothing turns it on for you.

If you want the information without the action, leave the watchdog in `report`
mode and read what it would have done. That costs nothing and carries no risk.

`sp account-auto-switch <session>` prints the decision for one session and
changes nothing; `--apply` is what actually moves it. Judging is the default
because this verb moves a live conversation between paid subscriptions and is
reachable from any shell in the checkout. The periodic check lives in the
watchdog and runs every
`SESSION_KIT_ACCOUNT_GUARD_SECONDS` (default `300`); the move itself is
performed only when the watchdog is in its acting mode,
`SESSION_KIT_WATCHDOG_MODE=repair`. In the default `report` mode the watchdog
says a session is on a spent account and changes nothing.

Being told is a debt, not an event. A completed move records what the owner is
owed before anything tries to deliver it, and the record is cleared only by a
delivery that actually succeeded — a notifier that is down costs a retry rather
than the message. `session_inventory.py account auto-pending-notices` lists
every move whose notice is still owed, oldest first, which is the hand-off
point for the configured delivery route. Automatic switching deliberately does
not choose a severity, device, or alert policy of its own.

The owner-only state sentinel
`$XDG_STATE_HOME/session-kit/account-switching-off`, defaulting to
`~/.local/state/session-kit/account-switching-off`, disables enrollment, new
account selection, manual switching, and automatic switching. It does not
delete profiles, change provider-owned login state, or stop running providers.
Session Kit never creates it: taking the manual switch away is an operator
decision, not a background-process side effect.

`configure-feeds` stores two owner-controlled absolute paths in the private
Session Kit state directory. Guided New uses a fresh roster for availability
and provider-qualified advice for recommendations. Missing or stale feeds leave
the choice unselected; they never guess an account.

## Inventory settings

Start from
[the inventory example](../config/session-inventory.example.json):

```json
{
  "schema_version": 1,
  "command_timeout_seconds": 6,
  "max_proc_nodes": 16384,
  "max_proc_depth": 32,
  "aliases": {}
}
```

Session Kit manages local aliases and automatic titles. Set names through the
command interface so identity checks and locking remain active:

```text
sp name <session-number|shpool-id> <title>
sp name reset <session-number|shpool-id>
```

Manual names take priority over provider titles, retained automatic names, and
fallback descriptions.

Renaming a session by hand also settles who owns its name, for good. Session Kit
records the rename in `name_ownership` alongside the aliases, and every
automatic naming path — the Claude title hook, the Codex auto-titler, `sp
self-name`, and any queued title retry that outlives a restart — reads that
record and leaves the session alone. `sp name reset` drops the name you gave and
keeps the ownership: automatic naming does not come back and quietly rename a
session you already named.

Automatic naming claims a name the same way, once, at a conversation's first
prompt. The claim is what makes the Claude hook a no-op on its second firing and
stops a later pass from re-titling a conversation an earlier pass named.
Pruning orphaned automatic titles clears spent automatic claims; it never
clears a rename you made.

## Project aliases

The initial interactive installer offers to import existing folders from both
providers. Review the same bounded discovery at any time:

```text
session-kit projects discover
session-kit projects candidates
session-kit projects import
session-kit projects import --select 1,3-4
session-kit projects list
session-kit projects add /absolute/path/to/web
session-kit projects add api /absolute/path/to/api
session-kit projects default api codex
session-kit projects normalize
session-kit projects here
session-kit projects ignore /absolute/path/to/scratch
session-kit projects unignore /absolute/path/to/scratch
```

`candidates` answers "what would import add, and under what name" without
writing anything. `import --select` takes only the entries you name, so a
first run is never all or nothing. `add` takes a directory and, if you want one
of your own, a short name; the provider is not asked, because which one opens a
directory is answered when a session starts. `default` records the provider a
project usually opens with, and `any` clears it again. `normalize` folds a
directory that is listed twice — the shape the old three-answer add produced —
into one entry, keeping the first short name. `here` adds the current directory
under a derived short name. `ignore` keeps a directory out of the picker, and
`unignore` takes that decision back so the directory is offered again.

The picker offers the same choices without any of these commands: press `m`
for More, then `p` for Projects, to add a directory, review the ones your
providers use that are not listed yet, drop one, or undo a drop with `u`.

Discovery reads only provider-owned local records. For Claude Code, those are
the project map in `~/.claude.json` and the project field in local history. For
Codex, they are project entries in `~/.codex/config.toml` and distinct working
directories in the newest local `state_*.sqlite` conversation database. Missing
directories and provider-configured temporary roots are ignored. Session Kit
does not scan unrelated folders.

Every directory gets at most one entry, whichever providers recorded it. A
directory is one project; the provider column on its row is a **default**, not
a lock, so `sp new codex web` opens the same directory with the other provider
without a second row. Adding a directory that is already listed is refused by
name rather than given a second entry. Alias collisions are resolved
with parent-folder or provider suffixes. Import is repeatable: directories
that already have a row — including an `ignore` row — and all hand-written
lines are left unchanged. Before appending any new rows, Session Kit saves an
owner-only backup under its state `backups` directory.

A row whose provider is `ignore` is a decision, not a shortcut. It is never
listed in the picker, never resolves as a launch target, and because import
gives each directory at most one row, discovery can never offer that
directory again. Adding the directory back withdraws the ignore.

The projects file is tab-separated:

```text
# alias<TAB>default provider<TAB>absolute cwd
web	claude	/absolute/path/to/web
api	codex	/absolute/path/to/api
tools	shell	/absolute/path/to/tools
docs	any	/absolute/path/to/docs
```

Aliases use lowercase letters, numbers, `_`, or `-`. The middle column is the
project's default provider — `claude`, `codex`, `shell`, or `any` for a project
that keeps no default. The directory must be absolute and exist when used.

```text
sp new web
sp new codex web
sp new shell tools
sp new claude docs
```

`sp new web` opens the default; naming a provider opens the same directory with
that one instead. A project whose default is `any` is never opened by guess:
`sp new docs` says which words start it rather than falling through to a shell.

A row is the host's own shortcut, and adding one is also what lets a project's
committed `session-kit.toml` decide the provider, account, model, and startup
command a launch uses. See [Projects](projects.md).

## shpool settings

Review [the shpool example](../config/shpool.example.toml). Session Kit expects
a bounded rendered restore buffer:

```toml
session_restore_mode = { lines = 500 }
output_spool_lines = 1000
vt100_output_spool_width = 200
prompt_prefix = ""
```

On macOS, installation also records the resolved Homebrew Bash executable in
this file. Session Kit refuses a different existing `shell` value. The launchd
shpool job receives this exact configuration path explicitly because shpool's
native macOS default is under `~/Library/Application Support`.

## Journals

Terminal journals are off by default. Opting in affects new managed sessions;
it does not add or remove a writer inside a session already running.

The `$HOME/.no_shpool_journal` sentinel forces journals off. Removing it does
not delete existing files and does not change an active terminal.

When a session with a journal is opened or reattached, the tail of its
rendered journal (the settled text `sp history` pages, never raw bytes) is
replayed into the terminal first, so the window starts with scrollback
instead of only the live frame. `SESSION_KIT_ATTACH_HISTORY` bounds the
replay; `0` disables it.

Closing or pruning a session does not remove its recording. Nothing ages
recordings out, so a busy machine accumulates them: they are the largest thing
the kit writes. `session-kit doctor` reports the total and warns when part of it is older than
`SESSION_KIT_JOURNAL_RETENTION_DAYS` (30 by default). That setting is a
reporting bound only; no command deletes a recording on its own.

## Optional feature sentinels

```text
$HOME/.no_shpool_reaper    scheduled cleanup observer
$HOME/.no_shpool_watchdog  health reports
$HOME/.no_shpool_journal   journals for new managed sessions
```

Removing a sentinel re-enables that component only after its other
configuration checks pass.

The Bash integration itself is managed by `session-kit enable-login` and
`session-kit disable-login`. These compatibility commands add or remove the
guarded `kit` function and SSH hint; they do not enable automatic picker
startup.

## Advanced watchdog repair mode

The installed watchdog defaults to `report` mode. On Linux, the source also
accepts `SESSION_KIT_WATCHDOG_MODE=repair` as an explicit advanced opt-in. The
installer does not enable repair mode. macOS supports report mode only because
watchdog repair requires Linux daemon-thread evidence.

Repair mode can close a terminal that has direct evidence of an unrecoverable
shpool handoff failure and relaunch the exact provider conversation in a new
session. That can discard terminal-only state, interrupt child
processes if the proof is wrong, and depend on provider-native recovery being
available. Quiet output alone never qualifies. Review the owner-only watchdog
log and test manual `sp repair` before enabling this mode.

The watchdog accepts a handoff marker only when its journal PID and monotonic
timestamp belong to the current daemon generation, the marker is old enough,
and no later successful attach for that session cancels it. A marker with
unknown recent-output age is reported but never repaired automatically. A
process-wide mismatch between listed sessions and serving-thread count is also
reported once at manager level; it cannot identify a specific session and is
never used to repair one.

## Watchdog alerts

`SESSION_KIT_WATCHDOG_NOTIFY` is **unset by default, so the watchdog raises no
alert anywhere.** It detects and logs conditions either way, but without a
notifier the only record is the owner-only watchdog log and the picker banner —
nobody is told. Set it to an executable that accepts:

```text
--type=<id> --severity=warning --title=<subject> --body=<text>
```

`SESSION_KIT_WATCHDOG_ALERT_TYPE` sets the `--type` value and defaults to
`session-kit.watchdog`. Severity is always `warning`; an automatic repair must
never fire a critical.

Wire this up if you rely on the watchdog at all. Detection without a notifier
leaves the finding only in local state, so the first visible symptom may be a
failed attempt to open a session. Prefer a systemd drop-in over editing the
unit, so a reinstall cannot drop it:

```ini
# ~/.config/systemd/user/session-kit-watchdog.service.d/notify.conf
[Service]
Environment=SESSION_KIT_WATCHDOG_NOTIFY=/path/to/your/notifier
```

## Notice routing

The picker lists sessions waiting for you. Everything else that used to appear
there — a repair that failed, a session reported quiet with nothing changed, a
stalled session, a question another window is asking — is not a session waiting
for you, and it is routed instead of listed. The installed
`session_kit_notice` helper is that route, and the watchdog calls it.

**Where a notice goes.** To the terminal session you are already sitting in when
you are at the machine; to the away transport when you are not.

**The terminal leg only applies inside a managed session.** If you are working
in a terminal Session Kit does not manage — an ordinary SSH window, a local
shell, an editor's terminal — nothing is attached, so presence reads as away and
every notice takes the away transport. That is not a fault to fix: there is no
managed conversation to deliver into. It does mean that on a machine whose
operator usually works outside the kit, **the away transport is the only route
that will ever fire**, and leaving `SESSION_KIT_NOTICE_AWAY_CMD` unset means no
notice reaches anyone at all.

**How "at the machine" is decided.** Two conditions, both required: **exactly
one** session is attached, and **a person has typed in it** in the last few
minutes (`SESSION_KIT_NOTICE_PRESENT_WITHIN`, default 300s).

Nothing else counts, and in particular *provider output does not*. Output is
autonomous — a session prints for as long as its turn takes, with nobody in the
room — so a window you walked away from mid-turn would look more present than
one you were reading. The evidence used is INPUT: Claude Code's
`UserPromptSubmit` hook, which the kit already records per session, and which
only fires when a person submits a prompt. A session with no hook installed, a
Codex session, or one whose newest record is a notification rather than a
prompt, all read as no evidence, which is away.

`attached` on its own means only "open in another window" — no focus, no proof
anyone is there, and a session can stay wired to an SSH window that no longer
exists. Two attached sessions mean the machine cannot say which one you are in,
and a notice into a Claude session PROMPTS that conversation, so ambiguity takes
the away route rather than guessing. No snapshot, a stale one, nothing attached,
several attached, or no sign of typing all mean away — a duplicate notice costs
a glance, a missed one costs an evening.
`session_kit_notice presence` prints the verdict and sends nothing.

**The terminal route never writes to a terminal.** It uses the session's own
authenticated message socket, so it cannot corrupt a screen a provider is
drawing. Only Claude sessions are addressable that way today; an attached Codex
session, and a session with an open question of its own, both fall through to
the away transport rather than being interrupted.

The terminal proof uses one receipt of record on every path: a valid JSONL user
record with the exact message UUID that the kit's transcript renderer accepts.
A UUID substring in malformed, truncated, unrelated, or non-renderable bytes is
not a receipt. Confirmation parses only the bounded 256 KiB transcript tail;
when that window cuts its first line, only the partial line is discarded and
complete later records remain eligible. A socket `delivered` answer says only
that the frame reached the session's queue; it justifies waiting the same
bounded confirmation window but never promotes the attempt by itself. The
proof starts with the current same-target presence check immediately before
`send()`, brackets the
returned socket call with another current check, establishes the transcript
receipt, and only then starts a separate current same-target read. That final
read's attachment sample therefore cannot predate the receipt. A detach before,
inside or after `send()`, while transcript confirmation is pending, or after
confirmation all demote the attempt. Every current check refuses the recently
published inventory; that cache cannot describe this interval. A slow, failed
or uncertain manager answer also demotes the attempt.

The socket result is first written to `notice-latch.json` as **provisional**,
with `landed=false`; it suppresses no replay. Reconciliation promotes it to a
terminal `landed=true` only when the transcript receipt exists and the same
attachment is proved immediately before send and again by a collection begun
after that receipt. Otherwise
the away leg fires and replaces the provisional result with the reconciled
away result. This can deliver the same notice twice when the conversation
accepted a frame just as its terminal detached. That duplicate is deliberate:
a receipt proves conversation acceptance, not that a person remained at the
terminal, and uncertainty defaults to away rather than false success.

**One incident, one notice, and the watchdog decides what an incident is.**
Every notice is latched by its key and by the evidence behind it, in
`notice-latch.json`, and the key is the SESSION rather than the record — one
wedged session can write many records. Within an incident, a stronger statement
(ordinary silence, then the daemon's own handoff failure) is a new one and is
made; a weaker one is not. Across incidents the rule is the watchdog's own: it
treats output after its last record as recovery and writes a fresh record, so a
record written **after** the last thing said about that session is a new
incident and is said. There is deliberately no second definition of "new
incident" here — two rules over one event stream is how a real second failure
goes unmentioned. An attempt that did not land is never latched as delivered,
and only an entry explicitly marked `reconciled` with `landed=true` suppresses
a replay. A provisional entry is retried.

**One message, not one per item.** A sweep says everything new in a single
notice naming up to five sessions and counting the rest. A large backlog
therefore produces one bounded notice instead of one message per record.

**Age decides what gets named, never what gets said.** A record older than
`SESSION_KIT_NOTICE_MAX_AGE` (default 21600s, the watchdog's own quiet-report
window) is counted in the message rather than listed by name. It is not
swallowed: age is not acknowledgement, and converting an unresolved warning into
"said" on the strength of a clock is the silence-looks-like-success failure this
route exists to end.

**Unconfirmed is not delivered.** The terminal transport cannot prove a message
landed. Even its `delivered` result confirms only the session queue. Every
notice is therefore confirmed by finding it in the target's own transcript,
which is the sole receipt of record. Until that and the ordered attachment
checks reconcile, the record remains provisional. If either proof is absent,
the notice takes the away route instead of being recorded as delivered.

**An unwired away transport is loud.** With `SESSION_KIT_NOTICE_AWAY_CMD` unset
there is no away route at all: the log says `UNWIRED`, and a sweep that had
something to say and reached nobody **exits non-zero** so it cannot be mistaken
for a quiet night.

| Variable | Meaning |
| --- | --- |
| `SESSION_KIT_NOTICE=off` | route nothing at all (kill switch) |
| `SESSION_KIT_NOTICE_AWAY_CMD` | the away transport. Same `--type/--severity/--title/--body` contract as the watchdog notifier, so any conforming notifier fits — but it is never inherited from that notifier, because that one is usually a board and a board is not a person. Unset means there is no away route, and every attempt says so |
| `SESSION_KIT_NOTICE_TERMINAL=off` | never use the terminal route |
| `SESSION_KIT_NOTICE_QUESTION_AWAY=hold` | stop pushing another window's open question to the away transport. The default is `send`: a class with no route is the thing this exists to prevent. A held question is still recorded, logged and counted |
| `SESSION_KIT_NOTICE_PRESENT_WITHIN` | seconds since the last valid UserPromptSubmit after which the one attached session reads as a window left open (default 300) |
| `SESSION_KIT_NOTICE_CONFIRM_SECONDS` | how long to wait for a terminal notice to appear in the target's own transcript before treating it as unconfirmed and taking the away route (default 6) |
| `SESSION_KIT_NOTICE_MAX_AGE` | seconds a record may be old and still be named; older unresolved records are counted in the same notice (default 21600) |
| `SESSION_KIT_NOTICE_SNAPSHOT_MAX_AGE` | seconds a snapshot may be old and still answer the presence question (default 90) |
| `SESSION_KIT_NOTICE_LATCH`, `SESSION_KIT_NOTICE_LOG` | the router's own state and log |

`extras/notify-chat-card` is a worked example of an away transport that reaches
a phone through a chat bot rather than a desktop popup. Wiring it, on a machine
whose watchdog runs under systemd, is a drop-in and a restart:

```bash
mkdir -p ~/.config/systemd/user/session-kit-watchdog.service.d
cat > ~/.config/systemd/user/session-kit-watchdog.service.d/notice.conf <<'CONF'
[Service]
Environment=SESSION_KIT_NOTICE_AWAY_CMD=%h/.local/lib/session-kit/current/extras/notify-chat-card
Environment="SESSION_KIT_NOTICE_CARD_CMD=/path/to/your/chat-bot post-card"
CONF

systemctl --user daemon-reload
systemctl --user restart session-kit-watchdog.service
```

Check it took, rather than assuming:
`~/.local/lib/session-kit/current/bin/session_kit_notice presence` prints the
verdict and sends nothing, and the next sweep writes either `ROUTED … via away`
or `UNWIRED:` to `notice.log` in the state directory.

## Color

Session Kit uses color in two unrelated ways, and only one of them is
configurable here.

**Semantic color** marks provider, availability, danger, and
secondary text, each with one consistent category. The picker applies it only
when output is an interactive, supported terminal, and labels and symbols carry
the same meaning without it.

**Session color** gives each Claude Code and Codex session its own identity
color, so two rows on screen do not look alike. It is derived from the
conversation's identity, and the two providers draw from separate palettes.
The palettes themselves are fixed, because Claude Code accepts only its own
eight color names, but the choice within a palette is not: `sp color <target>
<color>` records a per-session override and `sp color <target> reset` removes
it. See [Session colours](usage.md#session-colours) for the behavior and
`sp color reconcile` for settling sessions that already share one.

Disable color entirely with either variable:

```bash
export NO_COLOR=1
export SESSION_KIT_NO_COLOR=1
```

## The terminal tab name

The kit puts the session's own name on the terminal tab, on both providers, so
the window you are looking at and the list you opened it from agree about what
it is.

It gets there three ways, because no single one covers every moment.

**When you enter a session** — `sp go`, `sp takeover`, a fresh `sp new`, and the
picker's own open and take-over — the kit writes the tab name itself, from one
function, so the kill switch and the scrub below apply to every one of them.
Leaving a session through the picker puts `session kit` back on the tab.

**Inside a Claude window** the name arrives through the kit's `sessionTitle`
hook, which Claude applies at `SessionStart` and at each prompt. That is what
carries a rename into a window that is already open.

**Inside a Codex window** Codex writes the title itself from
`tui.terminal_title`, and every kit-launched session passes the kit's item list
on its own command line, so your `~/.codex/config.toml` is never edited and
Codex windows you start yourself keep whatever it says. The deployed template
lives at `$CODEX_HOME/session-kit/terminal-title.toml`. It is owned by the
release and replaced on every install and update, like the kit's Codex themes —
to change what kit sessions put on the tab, change it in the kit's own
`config/codex/terminal-title.toml` and install, or turn the feature off below.

Restoring a conversation does not write a tab name, because a restore leaves
you at the picker or at a prompt rather than inside the session: naming the tab
there would label a terminal that is not in that session. The restored
session's name and colour are written into the provider's own store before it
launches, and its tab is named when you open it.

Do not set `CLAUDE_CODE_DISABLE_TERMINAL_TITLE`. It reads like a way to stop
Claude writing titles so the kit can own them, and it is not: it silences every
title write from that window, the kit's hook included. With it set, Claude
windows carry no tab name at all. `session-kit doctor` reports it under
`tab-title` when it finds it set.

To turn the whole thing off, on both providers:

```bash
export SESSION_KIT_TAB_TITLE=off
```

## Supported environment overrides

### Locations and commands

```text
SESSION_KIT_CONFIG
SESSION_KIT_STATE_DIR
SESSION_KIT_JOURNAL_DIR
SESSION_KIT_ARCHIVE_DIR
SESSION_KIT_JOURNAL_RECOVERY_DIR
SESSION_KIT_JOURNAL_RETENTION_DAYS
SESSION_KIT_PROJECTS_FILE
SESSION_KIT_SHPOOL_CMD
SESSION_KIT_CLAUDE_CMD
CLAUDE_CONFIG_DIR
SESSION_KIT_CODEX_HOME
CODEX_HOME
SESSION_KIT_CODEX_DB
SESSION_KIT_BOOT_ID_FILE
SESSION_KIT_AUTO_NAME
SESSION_KIT_PICKER_EVENTS
SESSION_KIT_PICKER_PULSE
SESSION_KIT_CLAUDE_SOCKET
SESSION_KIT_NO_COLOR
SESSION_KIT_NONINTERACTIVE
SESSION_KIT_WATCHDOG_NOTIFY
SESSION_KIT_WATCHDOG_ALERT_TYPE
SESSION_KIT_WATCHDOG_MODE
SESSION_KIT_WATCHDOG_LOG
SESSION_KIT_WATCHDOG_REPAIRS
SESSION_KIT_WATCHDOG_BINARY_FINGERPRINT
```

`SESSION_KIT_ARCHIVE_DIR` and `SESSION_KIT_JOURNAL_RECOVERY_DIR` are two
different places, and both appear in the paths table above:
`shpool-archive` is what `sp find` searches, `shpool-journal-recovery` is what
exact recovery reads.

### Operator tunables

These change timing and thresholds. Every one has a working default; set them
only when the default is wrong for your machine. Values are what the code
reads today.

| Name | Default | What it changes |
| --- | --- | --- |
| `SESSION_KIT_PICKER_REFRESH_SECONDS` | `5` | picker live-refresh interval |
| `SESSION_KIT_PICKER_GROUP` | `state` | key-driven picker only: grouping it starts in — `state`, `provider`, or `project` |
| `SESSION_KIT_PICKER_COMPACT` | `0` | key-driven picker only: `1` starts it with compact rows |
| `SESSION_KIT_PICKER_FILTER_LIVE` | `1` | key-driven picker only: `0` disables filter-as-you-type; `/text` still searches on Enter |
| `SESSION_KIT_PICKER_PULSE_SECONDS` | `1` | how often the attention-pulse child scans while the picker is open; an unreadable value falls back to the default |
| `SESSION_KIT_PICKER_EVENT_POLL_SECONDS` | `30` | the timed poll interval while the live event stream is carrying attention; never narrower than the base refresh |
| `SESSION_KIT_ATTENTION_SOURCE` | `auto` | Claude attention evidence: `auto` merges the poll with the newer hook record, `poll` ignores hook records, and `hook` is for drills. Any other value falls back to `auto` |
| `SESSION_KIT_DOCTOR_FORMAT_SECONDS` | `10` | wall-clock budget for `session-kit doctor`'s internal-format scan; `0` reports the scan as timed out |
| `SESSION_KIT_TUI` | off | managed interactive Bash only: `on` makes its `kit` shell function open the interactive screen; a `tui-on` file in the state directory does the same. Direct executables and other shells remain key-driven |
| `SESSION_KIT_TUI_CMD` | the installed interactive picker | the interactive screen used by that managed Bash launcher |
| `SESSION_KIT_PICKER_FALLBACK_CMD` | the installed key-driven picker | where the managed Bash launcher goes when the interactive screen ends abnormally |
| `SESSION_KIT_TUI_FALLBACK_LOG` | `<state>/tui-fallback.log` | where a fall back is recorded, one line each |
| `SESSION_KIT_MODELS_FILE` | `~/.config/session-kit/models.tsv` | the models the picker's Change model action offers: one `provider<TAB>model` line each |
| `SESSION_KIT_STALL_SECONDS` | `2700` | silence after which provider activity no longer supports describing a session as `working` (clamped 60–86400) |
| `<state>/session-idle-minutes` | `30` | minutes a session that would otherwise say `needs you` must keep the same transcript path, size, and nanosecond modification time before it becomes `idle`. The file contains one positive number. `0`, an invalid value, a symlink, or any unreadable file disables idling; the kit never falls back to a shorter window after a failed read. Removing the file restores the 30-minute default |
| `SESSION_KIT_AUTO_CLOSE_HOURS` | `72` | continuous quiet hours before the cleanup observer may close a provider-exited terminal. A BACKSTOP: a session whose provider exits closes itself at once, so a terminal that reaches this window is one the shell could not close |
| `SESSION_KIT_PRUNE_DAYS` | `7` | age at which the reaper prunes its own records |
| `SESSION_KIT_REAPER_DRY_RUN` | `0` | `1` makes the reaper report what it would close and close nothing |
| `SESSION_KIT_SUBAGENT_IDLE_MINUTES` | `15` | quiet minutes before the sweep closes a sub-agent worker or background shell (TERM, then KILL one pass later). Workers are judged by movement of their own output transcripts; a long call that writes nothing can therefore look finished. The independent background-shell pass watches fd/1 and the shell's own CPU counters. Any live descendant refuses closure, including a sleeping zero-CPU child. Before signaling, the pass pins and stops a childless shell, verifies the stopped state, rechecks identity, zero descendants, own CPU, terminal foreground ownership, and fd/1, then sends the signal and resumes it. CPU is never evidence for closing a worker. The sweep runs every five minutes on its own `session-kit-subagent-sweep.timer`, so closure lands within about twenty. `0` turns the sweep off. **An unreadable value turns the sweep off and says so** — it never guesses a window. **Raise this if a real worker is ever closed while it is still working.** Exported variables do NOT reach the timer (the systemd user manager inherits none of your shell environment) — put the number in `<state>/subagent-sweep-minutes` instead, which the pass reads itself |
| `SESSION_KIT_SUBAGENT_IDLE_HOURS` | — | the older name, still honoured when the minutes name is unset, including `0` to disable; an unreadable value turns the sweep off. Linux only — the settled exact-PID delivery proof requires live procfs, so the sweep never runs on macOS. A reaper dry run previews sweep actions only after real passes have armed the idle clock, and touches nothing on disk |
| `SESSION_KIT_SUBAGENT_SWEEP` | on | `0`, `off`, `no`, or `false` turns the sub-agent sweep off; a `subagent-sweep-off` file in the state directory does the same |
| `SESSION_KIT_WATCHDOG_POLL_SECONDS` | `60` | watchdog pass interval |
| `SESSION_KIT_WATCHDOG_DEAD_SECONDS` | `2700` | quiet time before a terminal is treated as dead |
| `SESSION_KIT_WATCHDOG_FLAGGED_QUIET_SECONDS` | `120` | quiet time before an already-flagged terminal is acted on |
| `SESSION_KIT_WATCHDOG_QUIET_REPORT_COOLDOWN` | `21600` | minimum gap between repeat manager-level alerts (an unresponsive session manager, a replaced manager binary). It no longer governs per-session reports: a session gets one record per incident, and a second only when the session has produced output since the first or the watchdog has learned something stronger about it |
| `SESSION_KIT_WATCHDOG_MANAGER_TIMEOUT` | `20` | seconds the watchdog waits on a service-manager probe |
| `SESSION_KIT_WATCHDOG_RETIRE_DAYS` | `2` | age at which a watchdog record whose session no longer exists retires itself. A record is kept at any age while the session it names is live, while the REPLACEMENT it names is live (a repaired record points at the session still carrying the conversation), or while the published inventory still lists either — and nothing is retired at all unless the snapshot proved itself live. `0` turns retirement off |
| `SESSION_KIT_WATCHDOG_RETIRE` | on | `off` turns record retirement off |
| `SESSION_KIT_WATCHDOG_RETIRE_DRY_RUN` | `0` | `1` reports what retirement would remove in the watchdog log and writes nothing |
| `SESSION_KIT_ACCOUNT_ADVICE_MAX_AGE_SECONDS` | `600` | age past which account rotation advice is treated as stale |
| `SESSION_KIT_WORKTREE_ROOT` | `<state>/worktrees` | absolute directory holding materialized worktrees and their registry. Refused if it is under a system root — a working copy is never built where the machine keeps the software it runs |
| `SESSION_KIT_SHARED_REPOS` | unset | colon-separated absolute paths that are never copied: a delegated session runs in the checkout itself, and nothing is written into that repository. A `.session-kit-shared` file at a repository's root says the same thing, and travels with the checkout |
| `SESSION_KIT_COPYABLE_REPOS` | unset | colon-separated absolute paths that **are** yours to copy, overruling every reason the kit would otherwise refuse — the system-root rule included. Use it when a project genuinely lives under `/srv`, `/opt` or `/var` and is not the thing the machine is serving |
| `SESSION_KIT_CODEX_AUTOTITLE_BUDGET_SECONDS` | `2` | how long one pass of the Codex auto-titler may spend before the next inventory build; a backlog continues on the following pass (clamped above 0 and at 60) |
| `SESSION_KIT_ATTACH_HISTORY` | `500` | lines of the session's settled rendered journal replayed into the terminal's scrollback when a session is opened or reattached, so earlier output stays reachable by scrolling; `0` turns the refill off; a session without a journal replays nothing; an unreadable value falls back to the default |

### Off switches

Three behaviors can be turned off by an environment variable or by a file, so
they can be disabled for one command or for the machine. The file lives under
the Session Kit state directory.

| Behavior | Variable | File |
| --- | --- | --- |
| Session prebake | `SESSION_KIT_NO_PREBAKE=1` | `<state>/prebake-off` |
| Self-heal on start | `SESSION_KIT_NO_SELF_HEAL=1` | `<state>/self-heal-off` |
| Sub-agent sweep | `SESSION_KIT_SUBAGENT_SWEEP=off` | `<state>/subagent-sweep-off` |
| Sub-agent sweep, and every other closing pass | — | `~/.no_shpool_reaper` |
| Sub-agent sweep window | `SESSION_KIT_SUBAGENT_IDLE_MINUTES` | `<state>/subagent-sweep-minutes` |

`session-kit doctor` reports the sub-agent sweep as a state, on its own
`subagent-sweep` line: which window is in force and where it came from, which
switch is holding it off if one is, and — because a pass that never runs closes
nothing and says nothing — how long ago a pass last ran to completion. A sweep
that has gone quiet for more than twenty minutes is named there, whether the
cause is a held state-directory lock, a crashed pass, or a timer that is not
firing.

Shortening the window restarts every clock. The sweep records the window it
judged by alongside its evidence, and an idle clock gathered under a wider rule
is not evidence under a narrower one — so the first pass after any narrowing,
including the upgrade that introduced the fifteen-minute default, closes
nothing, and every worker is watched afresh for a full window. Widening keeps
the existing clocks, because a wider window can only ever close less.

The kit-owned tab name has its own switch, `SESSION_KIT_TAB_TITLE=off`, which
covers both providers; see [The terminal tab name](#the-terminal-tab-name).

The picker's three live channels are on by default and each is switched off by
`0`, `off`, `no`, or `false` (any case, surrounding spaces ignored). When one is
off, `session-kit doctor` names it on the `kill-switches` line instead of
reporting an all-clear.

| Channel | Variable |
| --- | --- |
| Push events into the picker | `SESSION_KIT_PICKER_EVENTS` |
| Attention-pulse watcher | `SESSION_KIT_PICKER_PULSE` |
| Claude in-session message delivery | `SESSION_KIT_CLAUDE_SOCKET` |

The `$HOME` kill switches `.no_shpool_reaper`, `.no_shpool_watchdog`, and
`.no_shpool_journal` are listed under
[Optional feature sentinels](#optional-feature-sentinels) and audited by
`session-kit doctor`.

A clean provider exit closes the managed session outright, on every machine —
there is no switch, and the retired `~/.sk_autoclose_on_clean_exit` marker is
no longer read anywhere. A non-zero exit is a crash and still stops at the
recovery menu with the terminal open.

### Everything else

`SESSION_KIT_TESTING` and the `SESSION_KIT_TEST_*` names are test seams, and
each one is read only when `SESSION_KIT_TESTING=1`; the same gate covers
`SESSION_KIT_PROVIDER_PRESENCE_OVERRIDE`. Setting any of them on a normal
installation changes nothing. The remaining `SESSION_KIT_*` names in the source are
internal: the installer and release engine pass them between their own
processes. They are not a stable public interface and are not listed here.
