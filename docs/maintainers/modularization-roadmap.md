# Inventory modularization contract

`lib/session_inventory.py` is the executable and import-compatible facade for
the inventory. Focused implementation lives in `lib/sessionkit_inventory/`,
but the split is intentionally incomplete: the facade still contains CLI
parsing, compatibility wrappers, and implementation retained by older tests
and callers.

This file records the boundary that future moves must preserve. The current
module inventory is in [Architecture](../architecture.md#inventory-package).

## Compatibility rules

- The facade remains executable and importable by path.
- Tested symbols, signatures, exit codes, JSON fields, and rendered behaviour
  remain available until a release explicitly changes their contract.
- Package modules never import the facade.
- Imports perform no process scan, lock, configuration read, or state write.
- A move preserves every test patch point or updates the test to patch the
  module that really resolves the name.
- Refactoring does not select a release, refresh a service, or mutate live
  session state.

## Patch-point ownership

A facade wrapper can inject collaborators at call time. In that case patching
the facade name still reaches the implementation. A package function can also
import a sibling directly; then the resolving package module owns the patch
point, and changing the similarly named facade attribute does nothing.

Use one of these proofs when writing a test:

1. patch the attribute on the module whose function resolves it; or
2. patch the facade and demonstrate interception with `wraps=` or an observable
   behaviour change.

Never assume that a re-exported name is an interception point. That mistake is
silent: the test runs against the real dependency and can pass without testing
the intended case.

Shared state helpers deserve particular care. Some modules accept locks,
paths, and publishers as injected arguments; others import the same helpers
locally. Inspect the call path before patching. Prefer call-time injection for
new state-moving code when it keeps recovery ordering visible and testable.

Constants need the same proof. A differential test using the real constant
cannot show that a patch stopped reaching its consumer. Change the constant in
the test and observe the result.

## Moving code safely

A coherent extraction follows this sequence:

1. freeze the symbol, signature, CLI, and patch-point behaviour;
2. move one focused responsibility without changing its output;
3. keep a facade wrapper where installed callers or tests import it;
4. prove normal import and direct file execution;
5. run focused tests for unsafe files, symlinks, interrupted writes, locks, and
   idempotency where state is involved;
6. run the full suite and public-export checks;
7. verify install and rollback with the package present and with a deliberately
   incomplete package refused.

Do not combine a file move with a user-visible behaviour change. A mechanical
move should have one answer to the question “what changed?”: only where the
implementation lives.

## Completion criterion

The long-term split is complete only when the facade contains compatibility
wrappers, CLI parsing, and `main`, no focused implementation remains there, no
package module imports it, and install, export, doctor, and rollback all enforce
package completeness.

That criterion is not met merely because a module with the right name exists.
The facade still has substantive implementation today, so future work should
describe each extracted responsibility rather than declaring the migration
finished as a whole.
