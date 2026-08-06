"""Terminal numbers: the retirement ledger and boot-stable selector assignment.

The registry file primitives live in ``state_io``; this module holds the policy
built on them — which identity owns a number, how long a dead number stays
reserved, and how a live inventory is numbered without disturbing rows.
"""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any, Callable, Mapping

from .common import PROVIDERS, CollectionError, valid_uuid
from .state_io import _read_bounded_owner_file, _terminal_generation_key
from .validation import _missing_shell_generation_is_quarantinable


# A dead session's number stays reserved this long before a new session may
# take it (Dan 2026-08-02: recycle-with-quarantine). A conversation recovered
# inside the window keeps its number via its surviving AI binding.
TERMINAL_NUMBER_QUARANTINE_SECONDS = 7 * 86400


def _terminal_ai_key(item: Mapping[str, Any]) -> str | None:
    identity = item.get("identity")
    provider = item.get("provider")
    if (
        provider in PROVIDERS
        and isinstance(identity, Mapping)
        and identity.get("confidence") == "exact"
    ):
        uuid = valid_uuid(identity.get("uuid"))
        if uuid:
            return f"ai:{provider}:{uuid}"
    hint = item.get("_terminal_identity_hint")
    if isinstance(hint, Mapping) and hint.get("provider") in PROVIDERS:
        uuid = valid_uuid(hint.get("uuid"))
        if uuid:
            return f"ai:{hint['provider']}:{uuid}"
    return None


def _read_terminal_retirements(
    path: Path,
    boot_id: str,
    *,
    max_bytes: int,
) -> dict[int, float]:
    """Retirement ledger: number -> unix time its last session disappeared.

    Kept OUTSIDE the registry file: pinned schema-v1 releases reject unknown
    registry keys, and standing windows keep running them. A boot change
    resets the ledger with the registry (numbers restart at 1 anyway).
    """
    raw_bytes = _read_bounded_owner_file(
        path,
        label="terminal retirement ledger",
        max_bytes=max_bytes,
        exact_mode=0o600,
        allow_missing=True,
    )
    if raw_bytes is None:
        return {}
    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(payload, Mapping) or payload.get("boot_id") != boot_id:
        return {}
    raw = payload.get("retired")
    result: dict[int, float] = {}
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            if (
                isinstance(key, str)
                and key.isdigit()
                and int(key) > 0
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
                and value > 0
            ):
                result[int(key)] = float(value)
    return result


def _terminal_retirement_payload(
    retired: Mapping[int, float],
    boot_id: str,
    *,
    schema_version: int,
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "boot_id": boot_id,
        "retired": {str(k): retired[k] for k in sorted(retired)},
    }


def apply_terminal_numbers(
    inventory: dict[str, Any],
    registry: dict[str, Any],
    *,
    boot_id: str,
    allocate: bool,
    retired: dict[int, float] | None = None,
    current_time: float | None = None,
    validate_registry: Callable[[Any, str], dict[str, Any]],
    schema_version: int,
    quarantine_seconds: float,
) -> dict[str, Any]:
    """Apply boot-stable selectors without changing contiguous internal rows."""
    checked = validate_registry(registry, boot_id)
    bindings: dict[str, int] = dict(checked["bindings"])
    next_number = checked["next_number"]
    recycle = allocate and retired is not None
    now = current_time if current_time is not None else time.time()
    if recycle:
        assert retired is not None
        # Bindings whose number finished its quarantine are pruned; the
        # number becomes allocatable below. Live numbers are never here.
        expired = {
            number
            for number, stamp in retired.items()
            if now - stamp >= quarantine_seconds
        }
        if expired:
            for key in [k for k, v in bindings.items() if v in expired]:
                del bindings[key]
            for expired_number in expired:
                retired.pop(expired_number, None)
    reserved_numbers: set[int] = set(bindings.values())
    active_numbers: set[int] = set()
    for item in inventory.get("sessions", ()):
        if not isinstance(item, dict):
            raise CollectionError("cannot number a non-object managed session")
        generation_key = _terminal_generation_key(inventory, item, boot_id)
        ai_key = _terminal_ai_key(item)
        if generation_key is None:
            if _missing_shell_generation_is_quarantinable(item):
                item["terminal_number"] = None
                item["mutation_allowed"] = False
                item["mutation_rejection_reason"] = "missing-shell-generation"
                item.pop("_terminal_identity_hint", None)
                continue
            if allocate:
                raise CollectionError(
                    "managed session lacks an exact generation for numbering"
                )
        ai_number = bindings.get(ai_key) if ai_key else None
        generation_number = bindings.get(generation_key) if generation_key else None
        generation_ai_key = (
            next(
                (
                    key
                    for key, value in bindings.items()
                    if key.startswith("ai:") and value == generation_number
                ),
                None,
            )
            if generation_number is not None
            else None
        )
        number: int | None
        if ai_number is not None:
            # Exact AI identity is the continuity proof when a recovered
            # conversation appears in a newly created shpool generation.  The
            # provisional number remains consumed, but the new generation is
            # rebound to the conversation's established terminal number.
            number = ai_number
        elif generation_number is not None and (
            ai_key is None or generation_ai_key is None
        ):
            number = generation_number
        elif generation_number is not None and not allocate:
            # The generation was already promoted to another exact
            # conversation. A read-only guard must not expose that number as
            # belonging to this unrelated identity.
            number = None
        elif allocate and recycle:
            # Lowest free number: anything not reserved by a live or
            # quarantined binding. Keeps numbers small forever.
            number = 1
            while number in reserved_numbers:
                number += 1
            reserved_numbers.add(number)
            if number >= next_number:
                next_number = number + 1
        elif allocate:
            number = next_number
            next_number += 1
        else:
            number = None
        if number is not None:
            if number in active_numbers:
                raise CollectionError(
                    "terminal number registry maps two active sessions to one number"
                )
            active_numbers.add(number)
            if allocate:
                assert generation_key is not None
                for key, value in tuple(bindings.items()):
                    if (
                        key.startswith("generation:")
                        and value == number
                        and key != generation_key
                    ):
                        del bindings[key]
                bindings[generation_key] = number
                if ai_key:
                    bindings[ai_key] = number
        item["terminal_number"] = number
        item.pop("_terminal_identity_hint", None)
    if recycle:
        assert retired is not None
        # Stamp newly dead numbers; clear stamps on anything alive again
        # (an exact recovery inside the quarantine window).
        bound = set(bindings.values())
        for number in bound - active_numbers:
            retired.setdefault(number, now)
        for number in active_numbers:
            retired.pop(number, None)
        # Legacy invariant for pinned releases: next_number stays above
        # every stored binding even after pruning shrank the map.
        top = max(bound, default=0)
        if next_number <= top:
            next_number = top + 1
    return {
        "schema_version": schema_version,
        "boot_id": boot_id,
        "next_number": next_number,
        "bindings": dict(sorted(bindings.items())),
    }
