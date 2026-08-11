# The Fleet Supervisor

One always-on session that knows your whole fleet. It sits first in the
picker, holds terminal **1** from the next reboot, and answers one question
better than anything else on the box: *what needs me right now?*

Open it two ways:

```text
v         from the picker — jumps straight into the supervisor
sp msg 1  message it like any session (it holds terminal 1)
```

`supervisor` itself is not installed on `$PATH` — `session-kit` installs
`kit`, `sp`, `shpool_login`, `shpool_status`, `shpool_reaper`, and
`codex_resume_here` only. `v` is the supported way in.

Ask it anything about the fleet: who is blocked on you (with the actual
question and how long it has waited), what finished that you haven't seen,
who is working, who has gone quiet. Every answer comes from a fresh read of
live state, stamped with its data age — it is architecturally forbidden from
answering out of memory.

## What it does on its own

Authority is earned, never granted. Every category of act sits in a lane:

- **Lane 1 — silent routine.** Compiling status, reading the queue. Logged,
  never announced.
- **Lane 2 — autonomous but surfaced.** Chasing a silent agent, answering a
  worker's factual question. It acts on its own and every act appears in
  your next brief.
- **Lane 3 — asks first.** Broadcasts, anything irreversible, anything it is
  unsure about. It refuses until you say yes in the conversation.

Twenty clean Lane-2 acts in a category make it *eligible* for promotion —
the brief tells you, and only you promote. One bad call demotes the category
to Lane 3 instantly. Autonomous acts are budgeted per UTC day (40 turns /
$5 estimated); past the cap it skips and says so. Every act — autonomous or
confirmed — lands in a private ledger you can audit, recording the lane it
actually ran under, whether you confirmed it, and whether it was surfaced to
you.

## The attention queue is yours too

The same ranked queue the supervisor reads is surfaced raw in the picker
header (`Needs you: 2 · … · a:review`) and via `sp msg queue`. If the supervisor is
ever down or wrong, the truth is one keypress away — it is never the only
path.

## A project you hand it outlives the resident

A project intake is written down before anything is delegated. It reaches the
spool two ways, and both write the same entry.

**Nobody has to message anything.** When a root session takes its first
substantive prompt — the moment you tell it what you want — the machine records
that prompt as an intake and asks for a supervisor in the background. No agent
cooperates, nothing is added to any agent's instructions, and your prompt waits
for none of it: the entry is on disk and `supervisor ensure` is already running
detached before the hook returns. One root thread produces exactly one intake,
however many times the hook fires; subagents and sidechains produce none;
greetings, slash commands, and the kit's own operator messages are not
projects. A supervisor that cannot start costs nothing — the project is on
disk, and the next supervisor reads it.

Both providers offer the same hook in the same place — in front of the prompt,
before the agent's turn — and the installer registers it for you:

| provider | file | registered in |
|---|---|---|
| Claude | `extras/hooks/sk_session_events.py` | `UserPromptSubmit` in `~/.claude/settings.json` |
| Codex | `lib/sessionkit_supervisor/provider_hooks.py codex-hook` | `UserPromptSubmit` in `~/.codex/hooks.json` |

Codex's is a *user-level* hook, which is what makes it general: a user-level
hook loads even in a project whose folder was never trusted, so a project
started anywhere is still recorded. Codex also trusts a command hook **by the
exact hash of the file it was shown** — so a release that changes the hook file
changes its hash, and Codex skips the changed hook until it is trusted again.
That re-trust is a real post-update step; `docs/update-and-rollback.md` says
when it applies.

What the tests here prove is the hook logic: one intake per root thread,
amendments, nothing for a subagent, nothing for a malformed payload, and a
prompt that is never delayed. They do not start a live Codex, so they cannot
prove that a fresh Codex process discovers, trusts, and invokes the hook —
that proof belongs to the end-to-end drill.

Each submitted root prompt is first recorded as an owner-private source event.
Its id hashes the provider, exact session UUID, submission key, and
SHA-256 of the exact raw UTF-8 prompt bytes before display normalization or
truncation. Codex uses its exact turn. Claude has no official turn field, so it
uses a digest of the exact pre-submit transcript file identity, length, and
final complete-record anchor. Intakes and amendments carry that id; amendment wake-ups carry only
the event and intake identifiers, never prompt prose as proof. The supervisor's
`verify_source_event` tool checks the private, no-symlink event and prefers an
exact provider-transcript match for session, turn, and raw prompt digest. If the
transcript has not landed yet, this installation may report the explicit
`hook-ledger` basis. That basis is useful local cooperative evidence, not
cryptographic identity against another process running as the same Unix user.
An ordinary `sp msg`, a forged id, a missing Codex turn, or a transcript mismatch
never authorizes an act.

### Headless launcher acceptance contract

The intake launcher distinguishes provider prompt acceptance from durable
intake acceptance. Before starting the provider it creates an owner-private
mode-0700 spool directory and exports the prompt digest, managed shell
generation, and a separate intake-commit path:

```text
SESSION_KIT_INTAKE_COMMIT_PATH=/absolute/private/path/prompt.intake_committed
SESSION_KIT_PROMPT_HANDOFF_SHA256=<sha256 of exact UTF-8 prompt bytes>
SESSION_KIT_MANAGED_GENERATION=<boot-id>:<shell-pid>:<start-ticks>:<started-ms>
```

Only after the source event and intake entry are fsynced does the hook atomically
publish the mode-0600 schema-v2 marker. It binds provider, conversation UUID,
submission key, prompt digest and byte count, source event, intake id,
requirements revision and digest, and managed generation. Provider acceptance
without this marker remains `intake_pending`, is never replayed to the provider,
and cannot prove a clean provider-exit lifecycle. A mismatch retains the item
for intake repair rather than claiming success.

**A project grows, and the record grows with it.** Only the first substantive
prompt opens an intake. Every later one, while that intake is still open, is
appended to it as an amendment and delivered to the supervisor once — the same
project, extended, never a second delegation. A prompt that arrives after the
project was reported opens a fresh intake instead, linked back to the one it
follows, because a finished thread that starts talking again is starting
something new.

**The other way is a message** — *"PROJECT INTAKE from terminal 7 …"* — for a
project handed over deliberately rather than typed into a root session.

Both land in `~/.local/state/session-kit/supervisor/intake/`, one file per
intake, written by the machine through these verbs and never by the resident's
hand:

```text
sp msg intake open                          what is still unfinished
sp msg intake record --msg-id … --source …  a messaged arrival, before planning
sp msg intake ack --msg-id … --text "…"     answer whoever raised it
sp msg intake preflight --msg-id …           record analysis and exact worker plan
             (also requires --analysis --scope --required-expertise
              --required-tags --worker-plan-json --risks --tests)
sp msg intake delegate --msg-id … --branch … who is carrying it
sp msg intake progress --msg-id … --text "…" tell the source how it is going
sp msg intake complete --msg-id … --text "…" tell the source it is done
sp msg intake duties [--msg-id …]           every duty and what it has said
sp msg intake report --msg-id … --branch …  a worker says completed or failed
sp msg intake retry --msg-id … --branch …   send a failed duty back to launch
sp msg intake reset --msg-id … --branch …   put a settled duty back to assigned
sp msg intake cancel --msg-id … --branch …  abandon one duty, with the reason
```

A new automatic intake also records a durable, identifier-only arrival notice.
If the resident already exists but is idle, the notice wakes that exact
supervisor under a stable delivery key; retrying the hook or the notice cannot
create another copy. A supervisor that is still starting reads the already
durable intake on its first turn, so the user's prompt never waits for either
startup or message delivery.

Each entry travels one way: received → acknowledged → delegated → reported.
*Reported* means the completion note actually landed in the source thread; one
that did not land leaves the project open, because a supervisor that meant to
tell you is not a supervisor that told you. Progress and completion go back
through the same `sp msg` machinery as everything else, recorded against the
entry with the idempotency key that carried them, so the same words are never
relayed twice and a note that missed is still owed.

Two arrivals of one intake share one entry. A message delivered twice, the same
project sent again under a new message id and the same intake key, or a hook
that fires a second time for a thread that already has an intake — each finds
the project already in hand. That is what stops a replacement resident from
delegating your project a second time. Every verb answers in machine JSON: the
entries carry thread keys and message ids, which belong in a record.

If the resident dies or is refreshed mid-project, the replacement's first read
is `sp msg intake open` — every intake still owing somebody something, oldest
first, read live from disk on every call.

Worker launch is a separate fail-closed state machine. The supervisor records a
preflight for the exact current requirements revision and digest before any
handoff. The preflight names the expertise the project requires, and the plan
must cover it: one worker for a one-skill project is a complete plan, and four
workers that between them miss a declared need is not. Nothing else is
prescribed — provider mix, model mix and worker count are the plan's business.
The complete plan is launched, not merely stored as metadata. Every assignment
keeps its idempotency key, workstream, scope, provider, requested and verified
actual model, expertise, rationale, branch, exact worker identity, and
not_started → dispatching → provider_reconciled → verified → commissioned
history. The complete plan is written under the intake lock before any launcher
callback. A crash leaves a dispatching assignment that retry reconciles without
relaunch; a new amendment or supervisor identity invalidates the old preflight
for unstarted work.

Every planned worker carries a duty: the task text it is sent, what finishing
means, and what it hands back. A verified worker is a proven session, not yet a
worker — commissioning is the step that delivers that duty to the exact proven
identity, recorded like any other send (reserved on disk under the key the send
will use, settled with what the receipt said). A duty that did not land leaves
the worker `verified`, names it in the delegate result's `undelivered`, and
makes the verb exit non-zero; the next delegate retries that delivery under the
same key rather than sending the work twice.

What became of a duty comes back the other way. `sp msg intake report` is the
worker's own channel: the report is written as a receipt file under
`intake/receipts/<msg_id>/` before the entry is touched, so a report that
exists cannot be lost by a failed entry write, and it does not depend on the
supervisor still being alive to hear it. `sp msg intake duties` is the view over
both directions — it flags a commissioned worker that has reported nothing for
half an hour as `silent`, a duty that never landed as `owed`, and a receipt on
disk with no matching line in the entry as an unfolded receipt. A duty is
`assigned`, `completed`, `failed` or `abandoned`; a worker reports the middle
two, `retry` sends a failed duty back to the start line under a new launch key,
`reset` returns a settled duty to assigned without relaunching, and `cancel`
abandons one with its reason recorded. Cancelling a duty closes the record for
that branch, not the worker's session.

Session Kit can gate only its supported delegation API. A provider-native spawn
or arbitrary Bash command can bypass that API because the cooperative Unix user
already has those capabilities. The standing brief forbids that route, and the
installed CLI delegation calls `sp new` with the exact provider, model, and
launch key, then ignores launcher stdout and separately requires one fresh live
inventory row with the same key, provider, model, and exact conversation UUID.
This is a cooperative policy boundary, not process isolation.

## Care and feeding

- `bin/supervisor ensure` creates it if missing (the picker's `v` does this
  for you). `refresh` hands off to a fresh context — same name, same
  number; the old resident writes a handoff, the new one reads it.
- It runs in your home directory; the directory must be trusted once
  (`claude` trust prompt) or a new resident parks before it can register.
- A new resident is briefed once, under one idempotency key, and `ensure`
  then waits for the resident's own proof that it took the brief: the brief
  in its transcript, or a turn it finished after the brief landed. A turn on
  its own proves nothing — anything can cause one — so it counts only behind
  a receipt saying the brief reached that session. Only a brief that
  affirmatively never landed is sent again. Once it has landed, the whole
  patience budget (about three minutes by default) goes on waiting — a
  newborn commonly needs over two minutes to reach its first turn boundary.
  If that budget runs out with no turn behind a landed brief, `ensure` exits
  3 and says so rather than delivering it again — open the session before
  sending anything else.
- A live session is not an ensured supervisor. The identity marker is
  published before the brief is composed, because terminal numbering needs
  it, so being briefed is recorded separately in `brief-proof`. An `ensure`
  that finds an identity without that proof finishes the earlier start
  against the same session — never a second session, never a silent success —
  and the idempotency key means a brief that already landed is not sent
  again while it waits.
- Its state lives under `~/.local/state/session-kit/supervisor/` — the
  machine writes it, and nothing else should. The ledger is append-only;
  `lanes.json` is the single authority on lane state.
- A refresh asks the resident for a structured handoff reply. Session Kit
  validates that exact reply and atomically writes `handoff.md`; the AI session
  never writes machine-owned supervisor state itself.
- Kill or close it like any session; `ensure` brings it back. Nothing else
  in the kit depends on it being alive.

## Known edges

- A freshly created Codex session has no identity until its first turn, so
  the supervisor cannot message it until someone (or something) has typed
  into it once.
- The event feed's `session_start` fires twice for a brand-new session (the
  name-applying relaunch is a real restart); anything counting starts should
  expect the pair.
