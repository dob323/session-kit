"""Glue between the `msg` subcommands and the verbs.

The facade owns the process — argument parsing, the process table, the live
snapshot — and hands each of them in, so this module has no ambient state and
one test can drive every verb against a sandbox.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Mapping

from .envelope import MessageError
from .ledger import Ledger
from . import claude_send, verbs

ACTIONS = (
    "resolve",
    "send",
    "report",
    "mark-read",
    "reply",
    "list",
    "unread-count",
)


def run(
    action: str,
    *,
    config: Mapping[str, Any],
    environ: Mapping[str, str],
    home: Path,
    snapshot_inventory: Callable[..., Mapping[str, Any]],
    process_table_reader: Callable[[Path, int], Mapping[int, Mapping[str, Any]]],
    prove_caller: Callable[..., Mapping[str, Any]],
    max_proc_nodes: int,
    target: str | None = None,
    text: str | None = None,
    fyi: bool = False,
    msg_id: str | None = None,
    thread: str | None = None,
    current_pid: int | None = None,
    idempotency_key: str | None = None,
) -> tuple[int, Any]:
    """Run one `msg` verb; returns (exit code, JSON-printable payload)."""
    if action not in ACTIONS:
        raise MessageError(f"unknown msg verb {action!r}")
    state_dir = Path(config["state_dir"])
    ledger = Ledger(state_dir)

    if action == "list":
        return 0, verbs.list_sends(ledger)
    if action == "unread-count":
        return 0, verbs.unread_count(ledger)
    if action == "report":
        return 0, verbs.report(ledger, msg_id, idempotency_key)
    if action == "mark-read":
        return 0, verbs.mark_read(ledger, thread or "")
    if action == "reply":
        if not msg_id:
            raise MessageError("a reply needs a message id")
        inventory = snapshot_inventory(write_state=False, config=dict(config))
        # The process-table root, and the SESSION_KIT_TESTING gate over its
        # fixture override, belong to the inventory. Imported locally so this
        # package still works on its own for every verb that needs no proof.
        from sessionkit_inventory.common import proc_root

        table = process_table_reader(proc_root(environ), max_proc_nodes)
        pid = current_pid if current_pid is not None else os.getpid()
        caller = prove_caller(inventory, table, environ, pid)
        outcome = verbs.reply(ledger, msg_id=msg_id, text=text or "", caller=caller)
        return (0 if outcome.get("ok") else 1), outcome

    if action == "send" and environ.get(verbs.MESSAGING_OFF, "").strip() == "0":
        # Refuse before the snapshot: an off switch must not reach the
        # providers at all, not even to list them.
        raise MessageError("mass messaging is switched off (SESSION_KIT_MSG=0)")
    inventory = snapshot_inventory(write_state=True, config=dict(config))
    if action == "resolve":
        registry = claude_send.registry_index(claude_send.registry_entries(environ))
        return 0, verbs.resolve(
            inventory, target or "", state_dir=state_dir, registry=registry
        )
    return 0, verbs.send(
        inventory=inventory,
        selector=target or "",
        text=text or "",
        fyi=bool(fyi),
        state_dir=state_dir,
        environ=environ,
        home=home,
        ledger=ledger,
        idempotency_key=idempotency_key,
    )
