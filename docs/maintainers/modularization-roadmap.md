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
