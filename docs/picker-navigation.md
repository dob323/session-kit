# Picker navigation

The session picker is the screen an SSH login lands on. This page covers the
keys that move around it — peeking at a session, filtering, jumping to whatever
is waiting on you, regrouping, and compacting the list — plus the optional
desktop notification for sessions that have been waiting a while.

Everything here is additive. The default screen, the default order, and every
existing key behave exactly as they did; `?` inside the picker prints the full
key table at any time.

## Peek at a row: `i<number>`

A row can say a session needs your reply without saying what it asked. `i3`
answers that in place:

```text
  Session 3 · Codex · duck · needs your reply
  parser refactor
  /srv/project
  Waiting 12 min for you.

  It asked
    Should I also update the changelog?

  Latest messages
    you · 3h 4m ago
      Refactor the parser please
    it · 12 min ago
      Should I also update the changelog?

  Type a reply and press Enter · o: open the session · Enter: back
```

Opening a peek changes nothing: it reads the attention queue in read-only mode
and the message thread the operator already has, so looking at a session never
marks it seen, never synthesizes an event, and never attaches.

Typing a reply sends it through `sp msg <number> "text"` — the same delivery,
ledger, and receipts the message centre uses. `o` hands the row to the ordinary
open path. Enter goes back to the list.

`i` on its own explains itself; a number that is not on the current page is
refused, exactly like every other numbered action.

## Filter as you type: `/text`

`/` has always searched names, providers, projects, IDs, and conversation
UUIDs. Now the list narrows while the line is still being typed: pause for a
moment and the screen shows what the half-typed search selects, with the typed
line still under it. Enter runs the identical search it always did.

A previewed filter belongs to the line being typed. Rub the line out, or turn
it into a different command, and the full list comes back before that command
runs — no action is ever taken against a list you only glanced at.

Set `SESSION_KIT_PICKER_FILTER_LIVE=0` to switch the preview off and keep the
submit-only search.

## Jump to what is waiting: `g`

`g` moves to the next session that is waiting on you or has finished unopened,
turning to its page and marking it with `▸`. Press it again for the next one;
it wraps at the end. The row is marked, never selected: nothing opens,
attaches, or is acknowledged by finding it. With nothing waiting it says so.

The Needs You screen (`a`) remains the full review surface — replies, prompt
deliveries, and repair failures as well as sessions.

## Group the list: `group`

| Command | Grouping |
|---|---|
| `group` | cycle state → provider → project → state |
| `group state` | ready to open / open elsewhere (the default) |
| `group provider` | Claude / Codex / managed shell |
| `group project` | one heading per project |

Grouping only decides which rows sit together. Inside every group the order is
the one the list has always used, and no row is added or removed.

Project grouping prefers the project an inventory row names for itself. Until
rows carry that, it derives the project from the session's working directory:
the alias from `projects.tsv` when the directory is inside a known project
root, otherwise the directory's own name, otherwise `No project`.

Start in a grouping with `SESSION_KIT_PICKER_GROUP=state|provider|project`.

## Compact rows: `c`

`c` drops the group headings and the trailing spacer and keeps only the primary
state beside each name, so more sessions fit on one screen. Same rows, same
numbers, same actions. `SESSION_KIT_PICKER_COMPACT=1` starts compact.

## Desktop notifications for waiting sessions (off by default)

The picker's Needs You screen is the guaranteed record of what is waiting. If
you also want a desktop popup when a session has been waiting a while, the
watchdog can send one through the notifier it already uses:

```bash
export SESSION_KIT_WATCHDOG_NOTIFY="$HOME/.local/bin/notify-desktop"
export SESSION_KIT_ATTENTION_NOTIFY=1
```

`extras/notify-desktop` is a working example of the notifier contract
(`--type`, `--severity`, `--title`, `--body`) using `notify-send` on Linux and
`osascript` on macOS. Copy it and change the last few lines to reach a phone,
a chat room, or an alert hub instead.

Restraint is the point:

- **Off unless asked for.** No variable, no notification.
- **Never critical.** These are always `warning`, so automation can never wake
  a phone that only pages on critical.
- **One alert per wait.** A session that keeps waiting is announced once; a
  *new* question from the same session is a new wait and is announced again.
- **Nothing under the threshold.** Default ten minutes, so a question answered
  in the same minute is never announced at all.
- **Read-only.** The queue is projected without mutating it, so an alert can
  never be what created the state it reports.
- **One switch to silence everything.** The watchdog sentinel
  (`~/.no_shpool_watchdog`) stops these as well.

| Variable | Default | Meaning |
|---|---|---|
| `SESSION_KIT_ATTENTION_NOTIFY` | `0` | `1` turns queue notifications on |
| `SESSION_KIT_ATTENTION_NOTIFY_AFTER_SECONDS` | `600` | how long a session must have been waiting |
| `SESSION_KIT_ATTENTION_NOTIFY_COOLDOWN_SECONDS` | `3600` | how often one unbroken wait may repeat |
| `SESSION_KIT_ATTENTION_ALERT_TYPE` | `session-kit.attention` | the `--type` value passed to the notifier |
| `SESSION_KIT_ATTENTION_NOTIFY_STATE` | `$XDG_STATE_HOME/session-kit/attention-notified.json` | which waits have already been announced |

## Picker view variables

| Variable | Default | Meaning |
|---|---|---|
| `SESSION_KIT_PICKER_GROUP` | `state` | grouping the picker starts in |
| `SESSION_KIT_PICKER_COMPACT` | `0` | `1` starts with compact rows |
| `SESSION_KIT_PICKER_FILTER_LIVE` | `1` | `0` disables filter-as-you-type |
| `SESSION_KIT_PICKER_REFRESH_SECONDS` | `5` | background refresh cadence; `0` disables it |

Grouping and compact chosen with a key last for that picker window. The
variables decide how the next one starts.
