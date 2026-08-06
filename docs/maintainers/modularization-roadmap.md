# Inventory modularization roadmap

`lib/session_inventory.py` remains the executable compatibility entry point
while implementation moves into `lib/sessionkit_inventory/`.

The current facade still combines configuration, Linux process inspection,
provider discovery, inventory assembly, state, naming, recovery, rendering, and
CLI parsing. Moving one bounded responsibility at a time makes review and
testing easier without changing installed command paths.

## Compatibility contract

Through at least one public minor release:

- `lib/session_inventory.py` stays executable and importable by path.
- Existing tested symbols, signatures, exit codes, JSON fields, and rendered
  behavior remain available.
- Extracted modules never import the facade.
- Imports perform no process scans, locks, configuration reads, or state writes.
- Existing test patch points continue to work.
- A refactor never moves the installed release link or restarts a service.

Patch-point scope after extraction: a package module may resolve a sibling
package symbol directly (the leaf-import convention), and from that moment
patching the same name on the facade no longer reaches package-internal
callers — the patch applies silently against nothing and the test stays
green against the real implementation. Every symbol an existing test patches
on the facade must therefore stay call-time-injected by its facade wrapper;
that set is verified per move. New tests must patch either the package module
attribute that is actually in the call path or a facade name proven to
intercept (a `wraps=` recording or an observable behavior flip), never an
unverified facade attribute.

### Package-only patch points

Names on this ledger are resolved inside the package. Patch them on the module
that resolves them; patching them on the facade reaches nothing and the test
stays green against the real implementation. This table is derived from the tree
by an AST scan rather than maintained by hand — a ledger that misdirects is
worse than no ledger, because it carries authority.

A name lands here two ways. It is **imported** from a sibling package module, or
it is **defined** in a package module that also has a facade wrapper, and a
sibling function in that same module calls the local definition rather than the
wrapper.

| Name | Resolved in | Patch on |
| --- | --- | --- |
| `_read_bounded_owner_file` | `names`, `terminal` (and `state_io`, which defines it) | the module whose read you are debugging — an alias-document read is `names`, a terminal-registry read is `terminal` |
| `_state_paths` | `names` | `sessionkit_inventory.names` |
| `StateLock` | `names` | `sessionkit_inventory.names` |
| `_codex_state_databases` | `names`, `names_push` | the module whose path you are on |
| `_ws_send_frame` | `names_push` | `sessionkit_inventory.names_push` |
| `_ws_recv_frame` | `names_push` | `sessionkit_inventory.names_push` |
| `_ws_request` | `names_push` | `sessionkit_inventory.names_push` |
| `_append_codex_index_entry` | `names_push` | `sessionkit_inventory.names_push` |
| `_codex_title_echoes_prompt` | `names_push` | `sessionkit_inventory.names_push` |
| `_push_codex_thread_title` | `names_push` | `sessionkit_inventory.names_push` |
| `_session_kit_state_dir` | `names_push` | `sessionkit_inventory.names_push` |
| `_first_text_block` | `self_name` | `sessionkit_inventory.self_name` |
| `_terminal_ai_key` | `terminal` | `sessionkit_inventory.terminal` |
| `_terminal_generation_key` | `terminal` | `sessionkit_inventory.terminal` |
| `_missing_shell_generation_is_quarantinable` | `terminal`, `validation` | the module whose check you are debugging |
| `_missing_shell_generation_is_quarantined` | `validation` | `sessionkit_inventory.validation` |
| `_display_width`, `_display_title`, `_format_age`, `stall_threshold_seconds` | `render` | `sessionkit_inventory.render` |
| `_positive_int`, `_proc_stat` | `reaper` | `sessionkit_inventory.reaper` |
| `_agent_identity`, `_base_agent`, `_shell_title`, `_empty_recovery`, `recovery_spec` | `collector` | `sessionkit_inventory.collector` |
| `_parse_shpool_payload`, `_is_native_claude`, `_is_native_codex`, `_codex_turn_state`, `_children_index`, `_process_age` | `collector` | `sessionkit_inventory.collector` |
| `_process_ancestor_chain` | `self_name` | `sessionkit_inventory.self_name` |
| `PROVIDERS` | eight modules | the module under test — it is a frozen set, so patching it anywhere is unusual |

The `common` primitives — `valid_uuid`, `clean_text`, `CollectionError`,
`PROVIDERS`, `automatic_naming_enabled`, `normalize_automatic_title` and the
`_valid_*` validators — are leaf-imported by nearly every module by design and
are not listed row by row. None is a patch point today.

**The split-treatment hazard.** `colors` takes `StateLock` and `_state_paths` as
injected arguments; `names` imports the same two directly. Both choices are
defensible on their own, and the inconsistency between them is the problem: a
facade patch on `StateLock` intercepts a colour write and silently does nothing
for a naming write, with nothing in either module telling you which side you are
on. Before patching a shared state helper, check which module owns the path you
are exercising. Prefer injection when adding a new consumer, so the set of names
that behave one way keeps shrinking rather than growing. `recovery` follows that
rule and injects both.

**A module can add no rows at all.** `recovery` and `snapshot` take every
collaborator as a call-time argument — the state reader and publisher, the lock,
the live collector, the strict live guard, the terminal helpers, the lifecycle
passes, their own siblings — so an AST re-derivation over the tree after both
moves produced the same names as before them, with the two new modules appearing
only under the `common` primitives below. Full injection is the right default for
the modules that rewrite state a person depends on immediately after losing work,
where a patch that lands on nothing is worse than a verbose signature. It is not
free: the signatures are long, and each new argument is a name a caller can pass
wrongly. Weigh that per module rather than copying either extreme.

`snapshot` gets a second benefit from it. That function is an ordering — take a
collection, and if it is complete publish the inventory, the terminal registry
and the recovery manifest under one lock, in the order a crash between any two
of them can survive. With every step arriving as an argument, the file reads as
the sequence itself rather than as a call graph you have to hold in your head.

Constants are the trap this ledger exists to record. A differential run with
every constant at its real value cannot detect a constant that stopped being
reachable: the behavior is identical, so the comparison is clean. Only patching
one and observing the result finds it. Four were caught this way —
`COLOR_RESERVATION_MAX_AGE_SECONDS`, `LAUNCH_COLOR_MAX_AGE_SECONDS`,
`TERMINAL_NUMBER_QUARANTINE_SECONDS` and `_TITLE_TRAILING_STOPWORDS` — and all
four were restored to call-time injection rather than listed here. The last of
them looked like a documentation case by every rule available (consumed only
in-module, unpinned, unread by any test) and turned out to change behavior when
patched. Patch it and watch; do not reason about whether it matters.

## Target package

| Module | Responsibility |
| --- | --- |
| `common.py` | constants, paths, normalization, configuration, command runner |
| `state_io.py` | private reads, locks, atomic writes, JSON and checksums |
| `processes.py` | Linux process table, ancestry, generations, boot identity |
| `providers.py` | shpool, Claude Code, and Codex readers |
| `model.py` | session records, titles, reply and provider-exit state |
| `collector.py` | bounded read-only joins |
| `names.py` | manual and automatic names |
| `terminal.py` | terminal-number and generation state |
| `validation.py` | strict snapshot and input validation |
| `recovery.py` | exact recovery transactions |
| `reaper.py` | 72-hour eligibility state and final safety proof |
| `snapshot.py` | collection and private-state orchestration |
| `render.py` | width, semantic color, dashboard, detail, JSON, and lookup |
| `self_name.py` | caller proof and automatic naming |

The dependency graph must stay acyclic. Recovery and cleanup accept explicit
callbacks rather than importing the facade.

## Sequence

1. Freeze facade behavior with symbol, signature, CLI, and patch-point tests.
2. Move pure common and configuration helpers.
3. Move private state I/O.
4. Move Linux process discovery.
5. Move provider readers.
6. Move the data model and collector.
7. Move validation and rendering separately.
8. Move naming and terminal-number state.
9. Move recovery and cleanup transactions.
10. Move snapshot and self-name orchestration.
11. Certify install, public export, rollback, and both private and public tests
    twice.

Do not combine file movement with a behavior change.

## Checks for each step

- compile the facade and package;
- run focused and full tests;
- import through normal import and direct file execution;
- verify the symbol and signature manifest;
- scan the public tree and reachable history;
- build the public export and prove package completeness;
- verify installer and doctor rejection of a partial package;
- compare frozen Linux fixtures;
- test unsafe modes, symlinks, interrupted writes, idempotency, and locks for
  state-moving work.

The split is complete when the facade contains compatibility wrappers, CLI
parsing, and `main`; no focused module imports it; export and install tools
enforce package completeness; and rollback passes from an immutable candidate.
