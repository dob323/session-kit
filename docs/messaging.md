# Message your sessions

Type `sp msg`. That opens the message centre — one screen where everything
happens: write to every agent, the idle ones, or a single one; watch their
replies arrive live; answer any of them. It exists for the moment you have a
dozen threads open and something to tell them. In the picker the same screen
is the `s` key.

Inside the centre:

```text
n        write a message: pick who (a=all · i=idle · a number), type it, done —
         the screen switches to that message and replies appear as they land
number   open a session's thread — the SAME number the main menu shows;
         type a reply at the prompt, an empty Enter goes back
a        follow up to every target of the message on screen (one confirmation)
l        recent messages, pick one to open
r        refresh now · Enter goes back (q works too)
```

Direct sends work too, for when you already know what you want to say:

```text
sp msg all "wrap up and commit what you have"
sp msg idle "status in one line please"
sp msg 14 "prioritize the sitemap fix"
```

Mass targets (`all`, `idle`) show the resolved target list and ask for one
confirmation; a single target sends immediately. `--fyi` marks a message as
informational — recipients are told no reply is wanted. `--yes` skips the
confirmation for scripted use by the operator; sends refuse to run
non-interactively without it.

## What a recipient experiences

- An **idle** session wakes, reads the message, acts on it, and replies.
- A **working** session sees the message at its next step and folds it in;
  nothing is restarted or interrupted, ever.
- A session **waiting on your answer** is excluded from `idle` (it is not
  free — it is blocked on you). Reach it with `all` or its number; its receipt
  row says it is waiting on you.
- Recipients reply with `sp msg reply <msg-id> "one line"` — the id is in the
  message they received.

## Delivery truth

Receipts are deterministic, not optimistic. A Claude delivery counts only when
the message is found in that session's own transcript; a Codex delivery counts
only when its App Server accepted the turn. Everything else is reported as
what it is: `unreachable` (a Codex session with no App Server socket, a Claude
session stuck on a folder-trust prompt), `ambiguous` (two sessions share a
display name — rename one with `sp name`), or `failed` with the exact error.
A target whose session exited between sends is skipped and reported, never
fatal to the rest. The pessimistic direction is possible: an `ambiguous`
receipt can under-report a message that did land (the reply arriving is the
proof) — a receipt never claims delivery it cannot prove, but absence of
proof is not always absence of delivery.

One state sits between proof and failure. `landed-unconfirmed` means the
sender handed the message to that exact session and the session's own
transcript has not shown it yet: a newborn or busy Claude session takes a
queued message at its next turn boundary, which can be minutes later. That is
an arrival. It is never counted as delivered, and it is never sent again.

## Repeating a send without repeating the message

A script that has to retry — because a receipt cannot prove what a queued
message did — sets `SESSION_KIT_MSG_KEY` for that one command:

```text
SESSION_KIT_MSG_KEY=supervisor-brief:main2 sp msg 2 "the standing brief" --fyi --yes
```

The key names a purpose, not an attempt. A repeat under the same key resumes
the message id that purpose already has instead of writing a second message,
and skips every target the ledger already shows as landed — including
`landed-unconfirmed` ones. Before dispatching to a Claude target again it
re-reads that target's transcript, so a message that arrived after the first
receipt closed is recognised rather than sent twice. Changing the words is a
different message and gets its own id. `sp msg report` shows what one purpose
sent (`msg report --key <key>` on the core). Keys are 1-128 characters of
`A-Z a-z 0-9 . _ : -`, and there is no operator flag for one: typing a message
means sending a new message.

## An intake the supervisor must not lose

A project handed to the fleet supervisor arrives as a message, and a message
lives only as long as the session reading it. `sp msg intake` writes that
project to a durable spool instead: one entry per intake, recognised rather
than repeated when it arrives twice, and relaying its progress back to the
source through this same delivery path. Docs: `docs/supervisor.md`.

## Replies and the ✉ mark

`✉` marks a reply you have not opened; unreplied targets sort first. Opening a
thread clears its `✉` — the same mark the picker counts, so the picker's
"✉ N new replies" header cue and the centre never disagree. `sp msg report
[id]` opens the centre on a specific message; in a pipe, a cron, or with
`SESSION_KIT_MSG_CONSOLE=0` it prints one static report and exits.

## Reach, cost, switches

Codex sessions are reachable when they run with the kit's App Server socket.
With `"codex_app_server_all": true` in `~/.config/session-kit/coordination.json`
every new kit-launched Codex session gets one regardless of project; sessions
started before that setting (or plain-TUI Codex) stay unreachable for their
lifetime and are reported as such. One more Codex edge: a freshly launched
session has no conversation identity until its first turn (identity comes
from its rollout file), so it reports "unknown provider … has no message
path" until someone has typed into it once. Claude sessions are reachable in
every project, including ones already running — but a session launched in a
directory whose trust prompt was never accepted parks before it can register;
its receipt says "not registered (possible trust prompt)", and launching from
a trusted directory is the fix.

A broadcast that wakes N idle agents spends N turns of their own provider
subscriptions, plus one short helper run (model overridable with
`SESSION_KIT_MSG_SENDER_MODEL`). The confirmation shows the count before
anything is spent.

Kill switches: `SESSION_KIT_MSG=0` disables sending entirely;
`SESSION_KIT_MSG_CLAUDE=0` / `SESSION_KIT_MSG_CODEX=0` disable one provider's
delivery (those targets report as unreachable, naming the switch).

Messages, receipts, and replies live under
`~/.local/state/session-kit/messages/` (private to your user, pruned after 90
days or 500 messages). Sending never attaches to a session, never restarts
one, and never writes to a terminal.
