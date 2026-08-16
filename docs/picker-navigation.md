# Picker navigation

Type `kit` from an ordinary shell to open Session Kit's session picker. The
home screen keeps managed sessions, new-session choices, projects, closed
sessions, and help in one place.

## Reading the list

Sessions that are ready in this window appear before sessions open elsewhere.
Within each group the order is `question`, `needs you`, `working`, then `idle`,
followed by provider, newest activity, and stable session identity. Machine
sessions sit behind one counted row.

A session row protects the facts that matter most as the terminal narrows:
state, a nonzero subagent count, and last activity. The time may shorten to
`3h` or `now`, then disappear at the narrowest width. Metadata and long names
give up space first.

| State | Meaning |
| --- | --- |
| `question` | Claude has a blocking prompt open now. Codex does not claim this state yet. |
| `needs you` | The provider has finished its turn and is waiting for you. |
| `working` | The provider is driving the current turn. |
| `idle` | A needs-you transcript has not moved for the configured window. |

`pending` is the placeholder for a value the kit cannot read. It is not a
fifth state. The idle window is 30 minutes by default; an unreadable
`<state>/session-idle-minutes` file disables the `idle` label instead of
guessing.

The model cell shows the conversation's current model when the provider record
supplies one, so it follows an in-session model change. A shell has no model.
Any model or account value the kit cannot read says `pending`.

## Enter and back

Enter takes the most likely choice throughout the picker:

- at home, it opens the top session; an empty session list opens New session;
- New session starts with Claude Code selected;
- a session open elsewhere defaults to Move it here;
- a closed conversation defaults to Restore.

`b` backs out of a secondary screen. At the home screen, back means leave.
Escape first clears typed input, then closes the current panel, returns from a
secondary view, and finally leaves when nothing remains to clear or back out
of. `q` leaves from an unfiltered home screen, and `Ctrl-D` leaves from
anywhere.

Nothing asks for confirmation. Guards run before the action, and the next line
says what happened. A refusal changes nothing.

## The key-driven picker

The home footer begins `↵ open <number>` or `↵ new`. As space narrows, footer
segments survive in this priority: Enter, number selection, kill, new, more,
needs you, help, history, leave.

The main keys are:

| Input | What it does |
| --- | --- |
| Enter | open the first visible session, or start New session when none is listed |
| `<number>` | open that session, or show Move it here when it is open elsewhere |
| `k <numbers>` | close one or more visible sessions; lists and ranges work |
| `h <number>` | show history without opening the session |
| `n` | start a new session |
| `m` | open the More screen |
| `a` | show sessions that need you |
| `?` | show picker help |
| `b`, `q` | back or leave |

A close keeps the number you typed while the picker refreshes, so the result
and the next list still refer to the same selection. Searches match names,
providers, accounts, models, and projects; they do not promise to match a
translated state word.

## The cursor-driven picker

The cursor-driven screen adds arrows, Page Up, Page Down, Home, End, marks,
live filtering, and mouse input. Digits create marks; commas and inclusive
ranges such as `1,4,7-9` select a set. Enter on a marked set opens the action
panel with Close selected.

Letters filter the list as they are typed. Escape clears that input before it
backs out. The wheel scrolls. One click highlights a row and a second click
opens it; clicking the mark column toggles its mark. The footer's Enter hint is
clickable, but the other footer text is a key reference rather than a row of
buttons. Hold the terminal's selection modifier, commonly Shift, when copying
text while mouse reporting is active.

## Session actions

A regular session can offer:

| Action | What it does |
| --- | --- |
| Open | open a session ready in this window |
| Move it here | disconnect the other window and open the session here |
| History | read settled session history |
| Close | close the session and retain its recoverable conversation |
| Change account | move the exact conversation to another enrolled account |
| Change model | resume the exact conversation on a configured model |
| Rename | set the session name |
| Color | choose from the provider's palette |

A marked set offers only actions safe for every target; today that means
Close. A closed Claude or Codex conversation offers Restore first. A closed
shell has no provider conversation to restore, so it offers History only.

Change model reads `~/.config/session-kit/models.tsv`, one
`provider<TAB>model` entry per line. An empty file produces an empty panel; the
kit never invents a model name.

## Machine sessions

Drills, workers, and automation-created sessions are machine sessions. They
stay behind one counted row until expanded. The row also says when one of them
needs you.

Creation records the origin. `sp new --origin machine` declares automation;
`--origin human` declares a person. A new session started from inside another
managed session defaults to machine origin, while one started by the picker
defaults to human origin. Restore and repair preserve the recorded origin.

## Closed sessions

Closing records the provider, exact conversation, title, directory, and close
order in the private closed-session ledger. The picker deliberately shows
`login time unknown` rather than presenting the close timestamp as a login
time. Restore brings an exact Claude or Codex conversation back; shell rows
retain history only.

If the ledger exceeds the normal safety ceiling, the picker refuses the
ordinary path and names the bounded recovery command. No confirmation prompt
is needed because a successful close is recoverable from this list.

## Refresh and release changes

The list refreshes in the background. Search, page, grouping, compact mode, and
the jump marker survive an action. A picker that notices a newly selected
release reopens through the current launcher at a safe point and carries its
view forward. If the new target is degraded, the existing picker remains in
place.

The two picker implementations have separate layout engines but share the
inventory, state vocabulary, session order, and proof-bound actions. See
[Troubleshooting](troubleshooting.md#which-picker-kit-opens) for selection
and fallback details.
