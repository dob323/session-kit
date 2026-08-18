# Session Kit voice, the one way every surface speaks

This file is the contract for every string the kit can show a person:
picker, `sp` output, in-session lines (bashrc), installer, doctor, watchdog,
reaper, completion, help, docs. Agents building any surface use these words
and only these words. Tests may grep this file.

## Terms (one per concept, no synonyms anywhere)

| Concept | The word | Never again |
|---|---|---|
| A managed terminal + what runs in it | session | managed terminal, thread, conversation (see below), row |
| The exact provider chat a session carries | conversation | thread |
| The screen that lists sessions | the picker | session picker, login chooser, dashboard, session list |
| A session waiting on the person | needs you | waiting on you, needs your reply, wants you, Needs a |
| A session the model is driving | working | none |
| When a session last did something | last active | recent output, process age, opened, last response |
| The model a session runs | its product name (`Opus 5`, `GPT-5-Codex`) | the raw identifier (`claude-opus-5`), unless the kit does not recognise it, in which case the identifier is shown rather than a name being invented |
| Openable in this window | ready | available, ready to open, available to open |
| Attached in another window | open elsewhere | already open in another window/SSH window |
| Sessions the kit does not manage | outside the kit | outside shpool, other provider sessions, provider roots |
| Machine-origin sessions (drills, workers) | machine sessions | throwaway, worker rows (in UI copy) |
| Full readable record of a session | history | clean history, terminal history, raw history, journal replay |
| End a session | close | end, teardown (`kill` is allowed only in a footer key hint such as `kill k #`) |
| Bring a closed conversation back | restore | recover, resume (UI copy; CLI verbs unchanged) |
| Start a session | new session | create, launch, start (as UI nouns) |
| The directories new sessions offer | projects | none |
| The daemon that keeps sessions alive | the session manager | shpool |

Provider names are always `Claude` and `Codex`; a plain shell is `shell`.
`shpool` never appears in UI copy; on screen the daemon is the session
manager. Stall reasons, `unsurfaced`, `unanswered`, `silent`, are degrees
of **needs you** and render as that one state.

There are four readable state words, in priority order: `question`, `needs you`,
`working`, and `idle`. `question` means Claude has a blocking prompt open right
now; Codex rows do not claim it until the provider supplies equally exact
evidence. `needs you` is the person's turn. `working` is the model's turn.
`idle` is a needs-you session whose transcript has not changed for the full
configured window. The older notification-derived `idle` was retired because
Claude and Codex described the same parked prompt differently. That reasoning
still stands: the new `idle` is provider-neutral transcript-movement evidence,
never a vendor notification. `pending` is not a state; it is the placeholder
for a value the kit cannot currently read. An unreadable idle-window setting
disables `idle` instead of guessing a duration.

The mapping from collector evidence to those words is **total**. A status this
contract does not name, the collector's own `unknown`, or a word a future
provider release invents, reads as `pending`, never as itself. A collector
word has never been screen copy, and the day one reaches a row is the day this
contract has been broken.

A session row's state, nonzero subagent count, and one time are protected
before its metadata and name. The count sits directly after the state and
before the time. As space narrows, the time shortens to an identifiable token
such as `3h` or `now`; at the narrowest status tier it may disappear. The state
word remains first and outranks the count.

A session row carries exactly one time, and it answers one question: when did
this last do something. Two times on one row meant every row truncated at a
different point and the column stopped being a column. The process age is on
`sp detail`, where there is room for it.

That detail view also gives each live child shell or worker at least one hour
old its own row, with the child's age on that row. A child conversation without
an exact live process does not get an estimated process age.

Session order is one contract too: ready before open elsewhere; within each,
`question`, `needs you`, `working`, then `idle`, followed by provider, newest
activity, and the
stable session identity. The shell picker, `sp list`, and the TUI do not invent
their own tie-breaks.

## Grammar (one form per interaction)

- **Refusal:** state the fact, then the way forward, in one line.
  `There is no session 20 on this screen. Numbers shown here work.`
  Never bare imperatives ("Choose a number shown here"), never three styles.
- **Nothing happened:** exactly `Nothing changed.`, the only cancel wording.
- **Action confirmations (post-fact):** name what happened, past tense, one
  line: `Closed 3 sessions.` `Restored Release notes review.` Every action
  says what it did; no silent successes.
- **Errors:** `session-kit: <fact>.`, one prefix, stderr, exit codes per
  `sp help exit-codes`; a no-match selector is always exit 2 with one
  message: `session-kit: no session matches that selector`.
  Screen copy is the UI: it prints unprefixed on stdout. Only a genuine
  failure takes the prefix, and it goes to stderr.
- **Menus:** rows are `<thing>` plus an optional short clause. The default
  row is marked; Enter takes it. No bracketed letters, no `[y/N]`, no typed
  words, no confirmation steps of any kind, anywhere.
- **Going back and going on:** one grammar on every screen. `b` goes back,
  and Enter takes the recommended choice, the same key meaning the same
  thing from the home screen to the provider, project, and move-here choices.
  Home Enter opens the top row; when the list is empty it starts a new session.
  The provider chooser defaults to Claude Code. A session open elsewhere
  defaults to moving it here. A prompt taking a typed name does not claim `b`,
  because there `b` is a name.
- **Columns:** a list whose rows carry fields after a variable-width name pads
  that name to one column, measured from the rows actually on the screen and
  in terminal cells rather than characters. One wide character is two cells
  and one `len()`; counting characters is how the columns and their colours
  end up in different places.
- **Empty states:** one form, `<Thing>: none.` A count line disappears at
  zero rather than printing `0`.
- **Footer hints:** lowercase and middot-separated. The key-driven home footer
  is limited to keys it dispatches and begins `↵ open <number>` or
  `↵ new`; as width shrinks it retains segments in this order: Enter, `#`,
  kill, new, more, needs you, help, history, leave. Panel footers name Enter
  and `b` as the route back where those keys apply. The cursor-driven footer
  is an action legend: printable letters still enter the live filter unless a
  panel explicitly binds them, so its labels are not promises of direct
  one-key dispatch.
- **Tone:** short declarative sentences, present tense, name the thing then
  the action. No exclamation marks. No jargon a first-time user would have
  to look up. The same sentence never appears in two lengths on two screens.

## Hard rules carried from the maintainer's decisions

- No confirmation ever, for anything (D7). Guards run before actions.
  Mistakes are handled by restore, not by asking first.
- Enter always takes the marked default (D7).
- Typing digits marks sessions; any letter makes the input a live filter
  (D9, D12). Esc clears typed input, then steps back through the current
  screen, and leaves only after there is nothing left to clear or go back from.
- The default picker view lists only sessions the person started; machine
  sessions sit behind one counted row (D17).
- No screen prints a shpool ID or conversation UUID (existing invariant,
`sp help selectors`), including `bye` and every close line.
