# The session snapshot, what a picker may read

One JSON document describes every managed session. `sp list`, `sp detail`, the
login picker and any new screen read that one document, so a session cannot be
`needs you` on one screen and `working` on another.

Produce it with:

```
shpool_status --json          # the whole snapshot, refreshing state
shpool_status --strict-json   # the same document, or nothing if it is not exact
python3 lib/session_inventory.py snapshot
```

`sp list` has no `--json` of its own; `shpool_status --json` is that channel and
is what `sp` itself uses (`sk_snapshot_file`). The login picker reads exactly
this document (`lib/sh/shpool_login_view.sh` projects it into a view).

## The one rule

**Additive only.** The picker a person is looking at parses this document live.
A renamed or removed field blinds that screen. Fields are added; they are never
renamed, retyped, or dropped.

## Document

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | int | `1`. A reader that does not recognize it stops. |
| `generated_at` | string | UTC ISO-8601 of the collection. |
| `collection_start` | object | Persistent positive `sequence` allocated before process reading. A publisher refuses to replace any document stamped by a larger sequence. |
| `source` | string | `live`, `cache`, or `cold`. |
| `stale` | bool | `true` when the list is not live. Screens must say so and refuse actions. |
| `warnings` | list[string] | Why a collection was incomplete. Empty on a good live pass. |
| `daemon_generation` | object\|null | The session manager's own generation (`pid`, `process_start_ticks`, `boot_id`). |
| `sessions` | list[object] | Managed sessions. See below. |
| `outside_agents` | list[object] | Provider processes the kit does not manage. Rows only; no numbers, no actions. |

Before a sequence is handed to a collector, it is fsynced into both the normal
counter and a separate durable allocation floor. If the normal counter is later
corrupt while that collector is still reading processes, rebuild starts above
the allocation floor rather than falling below the in-flight sequence.

Every collection-derived state document has a witness in the private
`collection-markers/` directory containing its sequence and the hash of its
exact incoming bytes. Publication writes and fsyncs that witness **before** it
atomically replaces the document. During that short interval the hash does not
match the incumbent document, but freshness checks still honor the witness's
sequence. A crash can therefore leave a newer witness with older document
bytes, never newer document bytes with no ordering witness; the next still-newer
publication repairs the pair. A missing or structurally unreadable witness is
replaceable with a diagnostic only while at least one durable allocation record
is readable. A document or marker with neither allocation record is evidence
of lost ordering state, not evidence of a first install.

`inventory-v2.lock` serializes new writers; the old `inventory.lock` becomes a
refusal fence so an in-flight pre-upgrade collector cannot publish after the
first marked collection starts.
The rollback manager detects a retained release without this fence-aware code,
publishes `inventory-rollback-v2`, durably restores a plain mode-0600
`inventory.lock`, and selects that release while it still owns
`inventory-v2.lock`. The rollback hold makes an already-waiting generation-2
publisher refuse read-only after the flip. It remains for the legacy release;
a newly launched, currently selected generation-2 publisher retires it on a
later forward update before recreating the refusal fence. The exact automatic
and emergency manual procedure is in
[`docs/update-and-rollback.md`](../../docs/update-and-rollback.md#inventory-publishing-fence).

Collection sequence allocation has two durable records: the ordinary counter
and its allocation floor. Sequence 1 is allocated only when both records, all
six collection-derived documents, and all collection markers are absent. If
either record remains readable, recovery advances above both that durable
allocation bound and every readable document marker before it writes a fresh
floor. If neither record can be read while any collection document or marker
exists, the collector does not infer a number. It returns the live reading
through the read-only path with a diagnostic and publishes nothing; a skipped
refresh cannot collide with an allocation still in flight. The offline reset
for an estate that has lost both records is documented in
[`docs/update-and-rollback.md`](../../docs/update-and-rollback.md#lost-collection-ordering-state).

## Session

Everything below is present on every session row. The first block is the
collectors' evidence and predates this document; the second is published on top
of it by `publish_view_fields()` in `lib/session_inventory.py`.

### Evidence (existing fields, never rename)

| Field | Type | Meaning |
|---|---|---|
| `terminal_number` | int\|null | The number a person types. `null` when the session could not be numbered. |
| `row` | int | Collection order. Not a selector; never shown. |
| `shpool_id`, `shpool_id_raw`, `display_shpool_id` | string | Machine identifiers. **No screen may print one.** |
| `availability` | string | `ready` or `attached`. |
| `shpool_status` | string | The session manager's own word (`Attached`, `Disconnected`). |
| `provider` | string | `claude`, `codex`, `shell`, `unknown`. |
| `display_provider` | string | The provider to show; wins over `provider`. |
| `title`, `display_title`, `native_title` | string | `display_title` is the one to show. |
| `cwd` | string | The session's directory, the project a row belongs to. |
| `identity` | object | `uuid`, `pid`, `process_start_ticks`, `confidence`, `provenance`. Machine-only. |
| `account_email`, `account_plan` | string | Enrolled account facts. |
| `agent_status` | string | The provider's own state word. Evidence, not copy, translate before showing. |
| `needs_you` | bool | The session is a person's turn. |
| `reply_optional` | bool | The model would take a reply but is not blocked. |
| `setup_incomplete` | bool | The launch never finished. |
| `started_at_unix_ms` | int\|null | When the session was opened. |
| `process_age_seconds` | int\|null | Age of the provider process (a resume restarts it). |
| `recent_output_at_unix_ms`, `recent_output_age_seconds` | int\|null | Last output seen. |
| `transcript_idle` | bool | True only after the conversation transcript's path, size, and nanosecond mtime remain unchanged for the full configured idle window. Missing or unreadable evidence is false. |
| `_transcript_idle_evidence` | object | Private carried clock: transcript `path`, `size`, `mtime_ns`, observation/movement times, and the window that gathered them. |
| `blocking_question` | bool | Claude only: exact unmatched top-level AskUserQuestion evidence, or an unresolved top-level tool use correlated by timestamp to the current permission-prompt hook. Always false for Codex until the app-server records already read can prove a live picker/approval. |
| `subagents` | list[object] | Child agents, each with a `status`. |
| `aged_children` | list[object] | Live child shells and workers at least one hour old, each with `kind`, a safe display `title`, and `age_seconds`. Detail-only; it does not affect state or `active_subagent_count`. A worker with no exact live process has no age and is omitted. |
| `active_subagent_count` | int | Subagents not idle/finished. |
| `is_subagent` | bool | Present and true when exact provider/process evidence proves this row is a child, including a child whose parent is unavailable. |
| `parent_session` | object | Optional exact `provider`, `uuid`, and `provenance` link to the parent. Machine-only; absent when child status is proven but the parent cannot be resolved. |
| `worktree` | object\|null | `branch` and paths when the session is isolated. |
| `display_color`, `color` | string | The session's theme colour name. |
| `provider_title_state` | string | `ready`, `pending`, `deferred`. |
| `recovery` | object | `available`, `provider`, `uuid`. |
| `diagnostics` | list[string] | Why a row is incomplete. |
| `mutation_allowed`, `mutation_rejection_reason` | bool, string\|null | Whether the kit may act on this session. |

### Published view (added by this pass)

| Field | Type | Meaning |
|---|---|---|
| `number` | int\|null | `terminal_number` when it is a usable selector, else `null`. The one number a screen shows. |
| `state` | string | One of `question`, `needs you`, `working`, `idle`, or the non-state placeholder `pending`. The mapping is TOTAL: a collector word with no entry reads as `pending`, never as itself. `idle` is based only on unchanged transcript evidence; vendor idle notifications still map to `needs you`. |
| `needs_you` | bool | As above, **plus** any fleet stall flag (see below). One meaning, decided here. |
| `needs_you_reasons` | list[string] | Empty, or the fleet's degrees: `unsurfaced`, `unanswered`, `silent`. |
| `attached` | bool | `availability == "attached"`. True means open in another window. |
| `age_seconds` | int\|null | Seconds since the session was opened. `process_age_seconds` remains the provider process's own age. |
| `subagent_count` | int | `active_subagent_count`, or the length of `subagents`. |
| `account_alias` | string\|null | The account a session runs on. `null` when there is none, show `, `. |
| `model` | string\|null | The model identifier a session is running NOW, read from the conversation's own record so it follows a `/model` typed inside the session. `null` when there is none to read. |
| `display_model` | string\|null | That identifier as a product name (`Opus 5`, `GPT-5-Codex`). What every screen shows. |
| `model_source` | string\|null | `transcript` when the conversation's own record answered. Nothing else sets a model: the launch argument is never presented as the live model. |
| `launch_model` | string\|null | What the process was STARTED with, as evidence only. Never shown as the model a session is running, `/model` does not touch argv. |
| `model_state` | string\|null | Why there is no model: `not-applicable` (a shell), `no-reply-yet`, `unreadable`. Screens turn it into a word; the row does not carry copy. |
| `origin` | string\|null | How the session was started, a person or a machine. `null` until a collector records one. This is what the row IS; for an unstamped session it can be a reading of the moment, so nothing durable may be derived from it. |

`project` is deliberately **not** published. The picker derives a project label
from `cwd` against `projects.tsv`, and a field of that name would silently
override it. Use `cwd`.

`outside_agents` rows carry `provider`, `display_provider`, `title`,
`display_title`, `agent_status`, `subagents`, `active_subagent_count`,
`identity` and `cwd`. They are not published through the view above: they have
no number, no state, and nothing to act on.

All picker surfaces sort session rows with the same published rule: ready
before attached; within each group, `needs_you` first, then
Claude/Codex/shell/unknown provider order, newest `recent_output_at_unix_ms`,
and natural `shpool_id_raw` as the stable tie-break. A nonzero
`active_subagent_count` is displayed directly after the state and before the
row's time, whether it came from provider evidence or grouped child rows.

### Present only when something proved them

These are written into published session rows, so a reader will meet them, but
they are deliberately **not** in the view above: each is absent unless the
evidence for it exists, and absence is the point. Test for presence; never read
one as `false` or `null` and treat that as a finding. Nothing in the picker's
own display depends on any of them.

| Field | Type | Meaning |
|---|---|---|
| `origin_recorded` | string | The stamp itself, present only where creation recorded one. Anything that writes provenance down for good (a repair that re-declares an origin, a close that fills the closed-session ledger) reads this, never `origin`. Its **absence** is what says nobody was ever recorded as this session's creator. |
| `machine_driven` | bool | Present and true only where collection proved that a program, and no window, holds this Codex App Server's socket. It is the one reading that can put an unstamped row behind the machine count, and it is refused outright unless the reading of the machine was complete. |
| `app_server_window` | bool | Present and true only when a window was positively seen holding this Codex App Server's socket. It is how a provider restart is known to have finished: the shell that relaunches a provider blocks on it and cannot report its own success, so it leaves its bounce marker behind and a sighting here is what settles it. |

## Fleet stalls fold into `needs_you`

`~/.local/state/fleet/stalls.json` (override the directory with
`SESSION_KIT_FLEET_DIR` for the inventory reader, `FLEET_STATE_DIR` for the
pulse pass, two readers, two variables) records sessions
the fleet believes are stuck:

```json
{"generated_at": 1786574881,
 "stalled": [{"key": "<uuid|shpool id|title>", "reason": "unsurfaced", "since": 1786569781}]}
```

`unsurfaced`, `unanswered` and `silent` are degrees of **needs you**
(`docs/voice.md`) and are folded into `needs_you` here, with the reasons in
`needs_you_reasons`. A fourth reason, `orphan`, is dropped: an orphaned session
is a dead session, not a person's turn.

The reader is bounded the same way the picker's is, file no larger than
256 KiB, at most 200 records, and ignored entirely once older than 300 seconds
(a flagger that stopped running is no evidence). A screen therefore never has to
read the fleet file itself, and the needs-you count on one screen can never
disagree with the rows on another.

## Words

Never spell a label into a screen. `lib/sessionkit_inventory/labels.py` holds
every word the kit shows a person, states, group headings, provider names, age
phrases, the placeholder `, `, the refusal and cancel lines, and
`tests/test_voice_labels.py` fails when a literal appears in a renderer instead.

| Need | Call |
|---|---|
| State word for a row | `labels.session_state(row, stall_seconds=...)` |
| Provider name | `labels.provider_name(row["display_provider"])` |
| Group heading | `labels.group_heading(row["availability"])` |
| Where it can be opened | `labels.where_word(row["availability"])` |
| `needs you 12 min` | `labels.waiting_phrase(seconds)` |
| `last active 3h 9m ago` | `labels.last_active(seconds)`, the one time a row shows |
| `5m`, `2h 3m` | `labels.duration(seconds)` |
| Missing value | `labels.MISSING` |
| Nothing matched a filter | `labels.NO_MATCHES` |
| No such number | `labels.no_such_session(number)` |
| Nothing happened | `labels.CANCEL` |
| A real failure, on stderr | `labels.error("...")` |

## Known drift, not yet converged

* `sp list` prints the collector's own status word (`running`) where the picker
  prints the translated one (`working`). `labels.STATE_WORDS` is the full
  translation and `labels.LIST_STATE_WORDS` is the subset `sp list` uses today;
  converging is a one-line change in `render.py` and a deliberate change to what
  `sp list` prints.
* `lib/sh/shpool_login_render.sh` carries its own copy of the state table and
  the age phrases, because it is bash and cannot import this module. The tests
  compare the two, so a change on either side is caught rather than shipped.
