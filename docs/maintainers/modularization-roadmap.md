# Inventory modularization roadmap

`lib/session_inventory.py` remains the executable compatibility facade while
its implementation moves into a sibling `lib/sessionkit_inventory/` package.
Callers keep using the same path, commands, exit codes, JSON fields, and
rendered output.

The source module currently has about 5,600 lines, 162 top-level definitions, and
several responsibilities:

- configuration and validation;
- Linux and macOS process inspection;
- Claude Code and Codex discovery;
- inventory assembly;
- private state and locking;
- naming and terminal-number persistence;
- recovery transactions;
- rendering;
- CLI parsing.

Source refactoring must not move an installed release link or restart a service.

## Compatibility boundary

Keep these rules through at least one public minor release:

- `lib/session_inventory.py` stays executable and importable by path.
- Existing constants, classes, public functions, and tested internal names
  remain available from the facade.
- `CollectionError`, `StateLock`, and Darwin ctypes classes keep one identity.
- Function signatures and CLI output stay compatible.
- Existing monkeypatch seams remain on the facade.
- Extracted modules never import the facade.
- Imports perform no configuration reads, process scans, locks, or state writes.

Simple imported aliases are not enough for symbols that tests and downstream
tools patch. Facade wrappers must pass the patched dependency into the focused
module. This applies to process identity, live collection, boot identity,
color selection, terminal sizing, recovery validation, and self-naming.

## Target modules

| Module | Responsibility | Depends on |
| --- | --- | --- |
| `common.py` | constants, types, paths, text normalization, config loading, command runner | standard library |
| `state_io.py` | private reads, locks, atomic writes, JSON and checksum helpers | `common` |
| `processes.py` | Linux and Darwin process tables, roots, descendants, generation and boot identity | `common` |
| `providers.py` | shpool, Claude Code, and Codex payload/process readers | `common`, `processes`, `state_io` |
| `model.py` | agent records, titles, output ages, retained launch attribution | `common`, `processes`, `state_io` |
| `collector.py` | `build_inventory` and `collect_live` read-only joins | `common`, `processes`, `providers`, `model` |
| `names.py` | aliases, automatic titles, audits, pruning, and alias migration | `common`, `state_io` |
| `terminal.py` | terminal-number registry and generation matching | `common`, `processes`, `state_io` |
| `validation.py` | strict and guarded inventory validation and input loading | `common` |
| `recovery.py` | pending recovery and transactional plan/apply/rollback logic | `common`, `state_io`, `processes`, `terminal`, `validation` |
| `snapshot.py` | collection plus terminal/recovery persistence orchestration | `collector`, `terminal`, `recovery`, `validation` |
| `render.py` | display width, age, color, inventory rendering, and lookup | `common` |
| `self_name.py` | caller proof and automatic self-naming | `processes`, `names`, `validation` |

The facade owns CLI parsing and calls down into these modules. The dependency
graph must remain acyclic. Recovery accepts collection and validation callbacks
instead of importing the snapshot facade.

## Phased commits

1. Lock facade behavior with symbol, signature, import, CLI, and patch-seam
   characterization tests.
2. Add the package marker and move pure common/config helpers.
3. Extract generic private state I/O and atomic writes.
4. Extract Linux, Darwin, and shared process discovery.
5. Extract provider readers with explicit identity callbacks.
6. Extract the inventory model, then the read-only collector.
7. Extract validation and rendering as separate leaf changes.
8. Extract naming state, then terminal-number state.
9. Invert recovery callbacks in place; test; then move the transaction unit
   without redesigning it.
10. Extract snapshot and self-naming orchestration.
11. Certify package completeness, public export, installation, rollback, and
    both private and public test suites twice.

Each phase is independently buildable and reviewable. Do not combine code
movement with behavior changes. Do not activate an intermediate refactor
release.

## Required checks at every phase

- compile the facade and every package module;
- run focused tests for the moved unit;
- run the full private and public suites;
- import through direct execution and `spec_from_file_location`;
- verify the tested facade symbol manifest and signatures;
- run the public privacy scanner;
- build the public export and prove every imported package file is included;
- verify installer and doctor checks reject a partial package;
- compare frozen Linux and macOS fixtures before and after the move.

State-moving phases also exercise unsafe modes, symlinks, replacement failure,
partial receipts, resume, rollback, idempotency, and concurrent locks.

## Completion criteria

The split is complete when the facade contains only compatibility wrappers,
CLI parsing, and `main`; no focused module imports the facade; package
completeness is enforced by release and installer checks; full tests pass twice;
and a new immutable candidate passes a dry-run rollback. Activation remains a
separate live-operation decision.
