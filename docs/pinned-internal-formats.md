# Vendor formats this kit depends on

Both vendors document these files as internal and free to change between
releases. The kit reads five file types and writes three of them, because the
interfaces that would replace those accesses either do not exist yet or cannot
be reached from a stdlib-only Python process.

That dependency is not the danger by itself. The danger is how it fails: a
format that changes shape says nothing. The reader finds no matching record,
returns its empty answer, and a title, a colour or a whole history quietly stops
working while every command still exits 0. So this page is the inventory —
every place the kit touches a vendor internal, the exact assumption it makes,
and what a person sees when that assumption stops being true.

`session-kit doctor` reads one live example of each and reports the row
`internal-formats`, naming every format and every field it could not confirm:

* **fail** when an example is present and a field a feature cannot work without
  has gone — at that point something a person depends on is already broken;
* **warn** when the file is still readable and a field named below as degrading
  rather than breaking has gone (`statusUpdatedAt`, `messagingSocketPath`,
  `thread_name`), or when no readable fixture is available — an unchecked
  format is not known to be healthy;
* **ok** only when all five were found and every field was there.

A file that is still being written — a transcript created a second ago, a line
cut off mid-append — is not a moved format, and never produces the fail. The
scan carries a wall-clock budget (`SESSION_KIT_DOCTOR_FORMAT_SECONDS`, default
10) and bounded traversal, so a slow or enormous home ends the check instead of
hanging doctor.

`tests/test_pinned_formats.py` fails when a kit file starts reading or writing
one of these paths without being listed here, so the inventory cannot go stale
in silence.

---

## Claude Code

### 1. The per-process session record — READ and WRITE

`~/.claude/sessions/<pid>.json` (per account profile, so also
`$CLAUDE_CONFIG_DIR/sessions/<pid>.json`).

| assumption | read by |
|---|---|
| a JSON object, one file per live process, named `<pid>.json` | `providers_claude._enrich_claude_payload`, `sessionkit_messages.claude_socket.find_target` |
| `sessionId` is the conversation uuid | both |
| `nameSource` says where the current window name came from | `providers_claude._enrich_claude_payload` |
| `name` is the title shown by Claude's own session picker | `providers_claude` (read), `names_push` (write) |
| `statusUpdatedAt` is Claude's own millisecond stamp for `status` | `providers_claude._enrich_claude_payload` → `attention.merge` |
| `messagingSocketPath` is the session's inbox socket | `claude_socket.find_target` |
| the file's mtime moves when the session's status transitions | `pulse.watched_paths` — stat only, parses nothing |

The reader recognises `bridgeSessionId, cwd, entrypoint, kind,
messagingSocketPath, name, peerProtocol, pid, procStart, sessionId, startedAt,
status, statusUpdatedAt, updatedAt, version`.

**Sanctioned alternative:** `claude agents --json` is the documented command and
the kit already runs it — but it returns seven fields only (`pid, cwd, kind,
startedAt, sessionId, name, status`), none of the four above. There is nothing
to migrate to today.

**When it breaks:** name provenance is lost (the kit stops distinguishing a
derived window label from a person's rename); the attention merge loses its
tie-breaker, so a hook record can no longer be aged out by a newer poll and a
record that says "needs you" stays authoritative until the hook replaces it;
socket delivery reports `no-socket-path` and falls back to the spawned sender;
and the pulse stops seeing status transitions, so the picker learns about a
waiting session on the timed poll instead of within a second. Nothing crashes; a
person sees stale titles, later attention and slower delivery.

### 2. The transcript JSONL — READ and WRITE

`~/.claude/projects/<slug>/<uuid>.jsonl`.

| assumption | used by |
|---|---|
| one JSON object per line, each with a `type` | every reader below |
| `{"type":"ai-title","aiTitle":…,"sessionId":…}` carries the conversation's auto-title | `providers_claude.read_claude_transcript_signals` (read), `self_name` (write) |
| `{"type":"agent-name","agentName":…,"sessionId":…}` carries a kit-assigned name | `providers_claude` (read), `names_push` (write) |
| `{"type":"agent-color","agentColor":…,"sessionId":…}` carries the session colour | `providers_claude` (read), `names_push` (write) |
| the file is append-only and safe to append to | `names_push`, `self_name` |
| `sp history` can render the whole conversation from it | `transcript_text` |

**Sanctioned alternatives, and why they are not in yet:**

* the **hook `transcript_path`** field is sanctioned, and
  `config/claude/nameintent_title.sh` already uses exactly that — it never
  guesses a path. That reader is already on a supported interface;
* `/export` is interactive only;
* the TypeScript SDK's `getSessionMessages` / `renameSession` / `tagSession`
  would replace the reads and the name write, but reaching them needs a Node
  process — the kit is stdlib-only Python by design, so this is a decision, not
  a patch;
* the messaging socket's rename control frame is available and
  `sessionkit_messages.claude_socket.rename()` implements it. It renames a
  RUNNING session through the vendor's own channel instead of appending an
  `agent-name` record to its transcript. Migrating the name push onto it is a
  possible alternative to the transcript write, but it changes the live
  delivery and fallback contract and therefore requires its own tested change.

**When it breaks:** `sp history` for Claude renders nothing (the Codex fallback
in `transcript_text` does not apply to a Claude conversation), auto-titles and
colours stop being recovered, and the name push appends records nothing reads.

### 3. The messaging auth key — READ

`~/.claude/sessions/<pid>.<sha256 of the resolved socket path>.key`, mode 0600,
`{"peerToken": "<32 hex>", "procStart": "…"}`.

Read by `claude_socket.read_peer_token`. The digest is over the **resolved**
socket path — `/run/user/<uid>/cc-socks/<pid>.sock` — rather than the spelling
of an unresolved link.

**Sanctioned alternative:** none; the token is what the protocol requires and
the file is where the vendor publishes it.

**When it breaks:** delivery reports `no-auth-key` and never sends blind, so the
caller falls back to the spawned sender. This is the failure mode this format
was designed to have.

---

## Codex

### 4. Rollout JSONL — READ

`~/.codex/sessions/**/rollout-*.jsonl`.

Read by `providers_codex` (turn state, bounded tail reads by descriptor) and
`transcript_text.render_rollout` (history). Assumption: JSON lines, each with a
`type` naming the event and a `payload` object under it. `pulse.watched_paths`
also depends on this path — stat only, because a Codex session waiting for an
approval appends a rollout record and raises no other signal.

**Sanctioned alternative:** the Codex **app-server** JSON-RPC exposes
conversation state, and the kit already speaks it for steering. Migrating the
history and turn-state reads onto it is real work with a live dependency on the
app server being up — it belongs to the app-server items, not to this page.

**When it breaks:** Codex history renders empty and turn state falls back to
silence-only evidence, which is exactly the Codex-side blindness the ledger
already records.

### 5. Session index — READ and WRITE

`~/.codex/session_index.jsonl`, read by `names`, `names_push` and
`providers_codex` to map a conversation id to its rollout, and stat-ed by
`pulse.watched_paths`. It is JSONL, with each entry carrying the conversation
`id` and `thread_name`. `names_push` appends a new exact-id row when it mirrors
or applies a title; the last row for an id wins.

**Sanctioned alternative:** the app-server's conversation listing covers the
same lookup, with the same live dependency as the rollout reads above.

**When it breaks:** a Codex session cannot be matched to its history, so titles
and history lookups miss.

---

## The rule for anything new

A new reader or writer of a vendor internal file is added to the table above in
the same change that adds the code, with: the exact path, the exact fields, the
sanctioned alternative (or "none today"), and what a person sees when the
assumption fails. `tests/test_pinned_formats.py` enforces the first half of
that; the rest is the point.
