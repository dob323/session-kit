"""The proposal surface: what the engine picked, and the numbers behind it.

Decision 10 of the overhaul plan asks for the math, not the verdict — chosen
*and* skipped accounts with their remaining numbers. An operator approving a
delegation plan has to be able to see why one account was preferred and why
another was passed over, without opening a log or trusting a summary.

So a proposal carries three things for every duty: the pick, every placement
that lost with its score broken down term by term, and the provenance of each
number that went in. A figure the kit inferred is labelled as inferred, a
figure a provider published is labelled as measured, and a window nothing could
read prints as unknown rather than as a confident zero.

``build_proposal`` produces the structure; ``render_proposal`` prints it for a
terminal. Nothing here spawns anything — this is the scoring and proposal layer
only, and the approval that acts on it lives elsewhere.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

from . import quota_sources as qs
from .duty_scoring import (
    Duty,
    ModelOption,
    Placement,
    ScoringError,
    Term,
    load_catalog,
    load_weights,
    score_duties,
)
from .quota_sources import AccountRef


PROPOSAL_SCHEMA_VERSION = 1
MAX_CONSIDERED_SHOWN = 6


def _stamp(value: int | None) -> str:
    if not value:
        return "unknown"
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _relative(value: int | None, now: int) -> str:
    if not value:
        return ""
    delta = value - now
    if delta <= 0:
        return "due"
    if delta < 3600:
        return f"in {delta // 60}m"
    if delta < 86400:
        return f"in {delta // 3600}h{(delta % 3600) // 60:02d}m"
    return f"in {delta // 86400}d{(delta % 86400) // 3600}h"


def build_proposal(
    config: Mapping[str, Any],
    duties: Sequence[Duty],
    *,
    refs: Sequence[AccountRef] | None = None,
    sources: Sequence[Any] | None = None,
    catalog: Sequence[ModelOption] | None = None,
    weights: Mapping[str, float] | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Read quota, score every duty, and return the plan with its evidence."""
    accounts = list(refs) if refs is not None else qs.account_refs(config)
    snapshot = qs.collect(config, accounts, sources=sources, now=now)
    options = list(catalog) if catalog is not None else load_catalog()
    term_weights = dict(weights) if weights is not None else load_weights()
    selections = score_duties(
        duties,
        accounts,
        snapshot,
        catalog=options,
        weights=term_weights,
        now=now,
    )
    return {
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "generated_at_unix": snapshot.taken_at_unix,
        "weights": term_weights,
        "catalog": [option.as_dict() for option in options],
        "accounts": [ref.as_dict() for ref in accounts],
        "quota": snapshot.as_dict(),
        "duties": [selection.as_dict() for selection in selections],
        "sources": sorted({reading.source for reading in snapshot.readings}),
    }


def _window_line(placement: Placement, snapshot_now: int) -> str:
    """One line carrying both windows' numbers for a placement."""
    parts: list[str] = []
    for term in placement.terms:
        if term.name not in ("weekly", "window"):
            continue
        label = "weekly" if term.name == "weekly" else "5h"
        if term.basis == "unknown":
            parts.append(f"{label} unknown")
            continue
        if term.basis == "relative-load":
            parts.append(f"{label} rank {term.value * 100:.0f}/100 (relative load)")
            continue
        # Print what was read, then the adjustment as an adjustment. A floored
        # window shown as "35% left (measured)" would put a provider's name on
        # a number the provider never reported.
        shown = term.raw_value if term.raw_value is not None else term.value
        line = f"{label} {shown * 100:.0f}% left ({term.basis})"
        if term.adjusted:
            line += f", scored as {term.value * 100:.0f}%"
        parts.append(line)
    reset = ""
    for reading in placement.readings:
        if reading.window == qs.FIVE_HOUR and reading.resets_at_unix:
            relative = _relative(reading.resets_at_unix, snapshot_now)
            if relative:
                reset = f", 5h resets {relative}"
            break
    return " · ".join(parts) + reset


def _back_when(row: Mapping[str, Any], now: int) -> str:
    """When a skipped account comes back, if anything said so."""
    for reading in row.get("readings") or []:
        if not reading.get("exhausted"):
            continue
        relative = _relative(reading.get("resets_at_unix"), now)
        if relative:
            return f" (back {relative})"
    return ""


def render_proposal(proposal: Mapping[str, Any]) -> str:
    """Render the plan for a terminal, numbers first."""
    now = int(proposal.get("generated_at_unix") or 0)
    duties = list(proposal.get("duties") or [])
    lines: list[str] = []
    noun = "duty" if len(duties) == 1 else "duties"
    lines.append(f"Delegation plan — {len(duties)} {noun}, quota read at {_stamp(now)}")
    sources = proposal.get("sources") or []
    lines.append(
        "quota sources: "
        + (", ".join(sources) if sources else "none produced a reading")
    )
    accounts = proposal.get("accounts") or []
    lines.append(
        "accounts considered: "
        + (", ".join(f"{row['provider']}:{row['alias']}" for row in accounts) or "none")
    )
    errors = (proposal.get("quota") or {}).get("errors") or []
    for error in errors:
        lines.append(f"  ! quota source error: {error}")
    for index, entry in enumerate(duties, start=1):
        duty = entry.get("duty") or {}
        lines.append("")
        lines.append(
            f"Duty {index}: {duty.get('title') or duty.get('duty_id')} "
            f"[{duty.get('work_type')}]"
        )
        chosen = entry.get("chosen")
        if chosen is None:
            lines.append("  → no eligible placement")
        else:
            placement = _placement_from_dict(chosen)
            who = f"{chosen['provider']}/{chosen['model']}/{chosen['account']}"
            email = f" ({chosen['email']})" if chosen.get("email") else ""
            lines.append(f"  → {who}{email}   score {chosen['score']:.3f}")
            lines.append(f"    {_window_line(placement, now)}")
            lines.append(f"    {chosen['rationale']}")
        considered = [
            row
            for row in (entry.get("considered") or [])
            if not (
                chosen
                and row["provider"] == chosen["provider"]
                and row["model"] == chosen["model"]
                and row["account"] == chosen["account"]
            )
        ]
        if considered:
            lines.append("  considered:")
            for row in considered[:MAX_CONSIDERED_SHOWN]:
                who = f"{row['provider']}/{row['model']}/{row['account']}"
                if row.get("excluded"):
                    back = _back_when(row, now)
                    lines.append(
                        f"    {who}  skipped — {row['exclusion_reason']}{back}"
                    )
                    continue
                placement = _placement_from_dict(row)
                lines.append(
                    f"    {who}  score {row['score']:.3f}   "
                    f"{_window_line(placement, now)}"
                )
            hidden = len(considered) - MAX_CONSIDERED_SHOWN
            if hidden > 0:
                lines.append(f"    … {hidden} more placement(s) scored lower")
        for note in entry.get("notes") or []:
            lines.append(f"  note: {note}")
    return "\n".join(lines)


def _placement_from_dict(row: Mapping[str, Any]) -> Placement:
    """Rebuild enough of a placement to render it from a serialized proposal.

    The renderer takes the serialized form so a proposal that travelled through
    a message or a file renders identically to one built in-process.
    """
    terms = tuple(
        Term(
            name=str(item.get("name") or ""),
            value=float(item.get("value") or 0.0),
            weight=float(item.get("weight") or 0.0),
            basis=str(item.get("basis") or ""),
            detail=str(item.get("detail") or ""),
            raw_value=item.get("raw_value"),
        )
        for item in row.get("terms") or []
    )
    readings = tuple(
        qs.QuotaReading(
            provider=str(item.get("provider") or ""),
            account=str(item.get("account") or ""),
            window=str(item.get("window") or qs.WEEKLY),
            source=str(item.get("source") or ""),
            confidence=str(item.get("confidence") or qs.OBSERVED),
            observed_at_unix=int(item.get("observed_at_unix") or 0),
            used_fraction=item.get("used_fraction"),
            resets_at_unix=item.get("resets_at_unix"),
            load_units=item.get("load_units"),
            load_unit=str(item.get("load_unit") or ""),
            exhausted=bool(item.get("exhausted")),
            detail=str(item.get("detail") or ""),
        )
        for item in row.get("readings") or []
    )
    return Placement(
        duty_id=str(row.get("duty_id") or ""),
        provider=str(row.get("provider") or ""),
        model=str(row.get("model") or ""),
        model_id=str(row.get("model_id") or ""),
        account=str(row.get("account") or ""),
        email=str(row.get("email") or ""),
        score=float(row.get("score") or 0.0),
        terms=terms,
        readings=readings,
        excluded=bool(row.get("excluded")),
        exclusion_reason=str(row.get("exclusion_reason") or ""),
    )


def parse_duty(value: str, index: int) -> Duty:
    """Parse ``work_type:title`` or ``id:work_type:title`` from the CLI."""
    parts = value.split(":", 2)
    if len(parts) == 2:
        work_type, title = parts
        duty_id = f"d{index}"
    elif len(parts) == 3:
        duty_id, work_type, title = parts
    else:
        raise ScoringError(
            f"duty must be work_type:title or id:work_type:title, got {value!r}"
        )
    return Duty(
        duty_id=duty_id.strip(),
        title=title.strip(),
        work_type=work_type.strip(),
    )


def _load_duties(path: str) -> list[Duty]:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise ScoringError("duties file must be a JSON list")
    duties: list[Duty] = []
    for index, row in enumerate(payload, start=1):
        if not isinstance(row, Mapping):
            raise ScoringError("each duty must be an object")
        duties.append(
            Duty(
                duty_id=str(row.get("duty_id") or f"d{index}"),
                title=str(row.get("title") or ""),
                work_type=str(row.get("work_type") or ""),
            )
        )
    return duties


def main(argv: Sequence[str] | None = None) -> int:
    """Score real accounts on this machine and print the proposal.

    Read-only: every source reads provider records and kit state, and nothing
    here writes, spawns, or switches an account.
    """
    parser = argparse.ArgumentParser(
        prog="python3 -m sessionkit_supervisor.quota_proposal",
        description="Score each (provider, model, account) for a set of duties.",
    )
    parser.add_argument(
        "--duty",
        action="append",
        default=[],
        metavar="WORK_TYPE:TITLE",
        help="a duty to place; repeatable (also id:work_type:title)",
    )
    parser.add_argument(
        "--duties-json", default="", help="path to a JSON list of duty objects"
    )
    parser.add_argument("--state-dir", default="", help="kit state directory")
    parser.add_argument(
        "--now", type=int, default=0, help="unix time to score against (testing)"
    )
    parser.add_argument("--json", action="store_true", help="print the raw proposal")
    args = parser.parse_args(list(argv) if argv is not None else None)

    duties: list[Duty] = []
    try:
        if args.duties_json:
            duties.extend(_load_duties(args.duties_json))
        for index, raw in enumerate(args.duty, start=len(duties) + 1):
            duties.append(parse_duty(raw, index))
    except (ScoringError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not duties:
        print("error: give at least one --duty or --duties-json", file=sys.stderr)
        return 2

    state_dir = (
        args.state_dir
        or os.environ.get("SESSION_KIT_STATE_DIR")
        or str(
            Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
            / "session-kit"
        )
    )
    config = {"state_dir": state_dir}
    try:
        proposal = build_proposal(config, duties, now=args.now or None)
    except ScoringError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(proposal, indent=2, sort_keys=True))
    else:
        print(render_proposal(proposal))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
