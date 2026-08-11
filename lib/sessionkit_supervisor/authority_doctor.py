"""Report the evidence behind this machine's source authority, and change none of it.

The acceptance instrument for the source-authority work: it answers "how much
of what this machine recorded could actually be proved, and by what", so a
policy is set against measured numbers instead of hope.

It records nothing. No verification receipt is written for any event it
inspects — that is what `verify_source_event(record=False)` is for — so running
the report never leaves behind receipts for verifications nobody asked for. The
source ledger's own crash-recovery step still runs, exactly as it does on any
read of an event.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys
import time
from typing import Any, Callable, Iterator, Mapping, Sequence

try:
    from sessionkit_supervisor.source_authority import (
        TIER_NAMES,
        TIER_UNVERIFIED,
        authority_for_intake,
        locate_transcript,
        machine_envelope_prompt,
        required_tier,
        verify_source_event,
    )
except ModuleNotFoundError:  # ``lib.sessionkit_supervisor`` test/import form
    from .source_authority import (  # type: ignore[no-redef]
        TIER_NAMES,
        TIER_UNVERIFIED,
        authority_for_intake,
        locate_transcript,
        machine_envelope_prompt,
        required_tier,
        verify_source_event,
    )


MAX_REPORT_BYTES = 2 * 1024 * 1024
DEFAULT_WINDOW_DAYS = 7
# The share of a window's operator intakes that must reach the delegate tier
# before a gate built on this evidence is worth turning on. Named here because
# the number is the decision, not an implementation detail.
ACCEPTANCE_SHARE = 0.9


def _read_private_json(path: Path) -> dict[str, Any] | None:
    """One owner-owned JSON object, or None. Never follows a symlink."""
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            return None
        if info.st_size > MAX_REPORT_BYTES:
            return None
        value = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    return dict(value) if isinstance(value, Mapping) else None


def _objects(directory: Path) -> Iterator[dict[str, Any]]:
    try:
        names = sorted(entry.name for entry in os.scandir(directory))
    except OSError:
        return
    for name in names:
        if not name.endswith(".json"):
            continue
        value = _read_private_json(directory / name)
        if value is not None:
            yield value


def source_events(state_dir: Path | str) -> Iterator[dict[str, Any]]:
    root = Path(state_dir) / "supervisor" / "source-events" / "entries"
    yield from _objects(root)


def intake_entries(state_dir: Path | str) -> Iterator[dict[str, Any]]:
    root = Path(state_dir) / "supervisor" / "intake" / "entries"
    yield from _objects(root)


def _operator_intake(entry: Mapping[str, Any]) -> bool:
    """Whether a person opened this intake, rather than the kit's own transport.

    A delivery-runner or wake prompt reaches the same spool and is not somebody
    asking for a project, so counting it in an acceptance number would measure
    the kit's own traffic instead of the operator's.
    """
    if entry.get("origin") != "auto":
        return False
    summary = entry.get("summary")
    if not isinstance(summary, str) or not summary:
        return False
    if machine_envelope_prompt(summary):
        return False
    try:
        from sessionkit_supervisor.intake import machine_transport_prompt
    except ModuleNotFoundError:  # ``lib.sessionkit_supervisor`` import form
        from .intake import machine_transport_prompt  # type: ignore[no-redef]
    return not machine_transport_prompt(summary)


def authority_report(
    state_dir: Path | str,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    thread: str = "",
    action: str = "delegate",
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Every number the source-authority acceptance gate is judged on."""
    state = Path(state_dir)
    now_ms = int(clock() * 1000)
    window_start = now_ms - window_days * 86_400_000
    wanted = required_tier(action)

    by_tier: dict[str, int] = {name: 0 for name in TIER_NAMES.values()}
    by_provider: dict[str, dict[str, int]] = {}
    unlocated: dict[str, int] = {}
    sessions: dict[str, tuple[str, str]] = {}
    newest: dict[str, Any] = {}
    named: dict[str, Any] = {}
    total = 0
    for event in source_events(state):
        event_id = str(event.get("event_id") or "")
        if not event_id:
            continue
        total += 1
        result = verify_source_event(state, event_id, clock=clock, record=False)
        tier = int(result.get("tier", TIER_UNVERIFIED))
        name = TIER_NAMES.get(tier, TIER_NAMES[TIER_UNVERIFIED])
        by_tier[name] = by_tier.get(name, 0) + 1
        provider = str(event.get("provider") or "unknown")
        row = by_provider.setdefault(provider, {key: 0 for key in TIER_NAMES.values()})
        row[name] = row.get(name, 0) + 1
        session = str(event.get("session_id") or "")
        key = f"{provider}:{session}"
        transcript = str(event.get("transcript_path") or "")
        if not transcript or not Path(transcript).is_file():
            unlocated[key] = unlocated.get(key, 0) + 1
            sessions[key] = (provider, session)
        recorded = event.get("recorded_unix_ms")
        stamp = recorded if isinstance(recorded, int) else 0
        summary = {
            "thread": key,
            "event_id": event_id,
            "tier": tier,
            "tier_name": name,
            "basis": str(result.get("basis", "none")),
            "evidence_root": str(result.get("evidence_root", "")),
            "recorded_unix_ms": stamp,
        }
        if stamp >= int(newest.get("recorded_unix_ms", -1)):
            newest = summary
        if thread and key == thread and stamp >= int(named.get("recorded_unix_ms", -1)):
            named = summary

    window_total = 0
    window_met = 0
    refusals: dict[str, int] = {}
    lead: list[dict[str, Any]] = []
    for entry in intake_entries(state):
        if not _operator_intake(entry):
            continue
        received = entry.get("received_unix_ms")
        if not isinstance(received, int) or received < window_start:
            continue
        window_total += 1
        verdict = authority_for_intake(state, entry, action=action, clock=clock)
        if verdict["allowed"]:
            window_met += 1
        else:
            code = str(verdict["refusal"]["code"])
            refusals[code] = refusals.get(code, 0) + 1
        if thread and entry.get("source_thread_key") == thread:
            lead.append(
                {
                    "msg_id": str(entry.get("msg_id") or ""),
                    "allowed": bool(verdict["allowed"]),
                    "min_tier_in_chain": int(verdict["min_tier_in_chain"]),
                    "chain_length": len(verdict["chain"]),
                    "refusal": verdict.get("refusal", {}),
                }
            )

    share = (window_met / window_total) if window_total else 0.0
    return {
        "generated_unix_ms": now_ms,
        "state_dir": os.fspath(state),
        "window_days": window_days,
        "action": action,
        "required_tier": wanted,
        "required_tier_name": TIER_NAMES.get(wanted, "unverified"),
        "events": {"total": total, "by_tier": by_tier, "by_provider": by_provider},
        "sessions_without_recorded_transcript": [
            {
                "thread": key,
                "events": count,
                # What the event recorded and what exists on this disk are
                # different questions. An event captured before a root was
                # known recorded nothing; the provider's file may still be
                # right here, which is the difference between "no evidence"
                # and "evidence this event cannot reach".
                "discoverable": locate_transcript(*sessions[key], state),
            }
            for key, count in sorted(unlocated.items())
        ],
        "operator_intakes": {
            "in_window": window_total,
            "at_required_tier": window_met,
            "share": share,
            "refusals": refusals,
        },
        "acceptance": {
            "target_share": ACCEPTANCE_SHARE,
            "met": window_total > 0 and share >= ACCEPTANCE_SHARE,
        },
        "newest_source_event": newest,
        "named_thread": {"thread": thread, "event": named, "intakes": lead}
        if thread
        else {},
    }


def render(report: Mapping[str, Any]) -> str:
    """The report as lines a person reads, in the doctor's own tab format."""
    events = report["events"]
    intakes = report["operator_intakes"]
    lines = [
        "source authority\t"
        + f"{events['total']} source events; "
        + f"{report['action']} needs tier {report['required_tier']} "
        + f"({report['required_tier_name']})",
    ]
    for tier in sorted(TIER_NAMES, reverse=True):
        name = TIER_NAMES[tier]
        lines.append(f"tier {tier} {name}\t{events['by_tier'].get(name, 0)} events")
    for provider in sorted(events["by_provider"]):
        row = events["by_provider"][provider]
        counts = ", ".join(
            f"{TIER_NAMES[tier]} {row.get(TIER_NAMES[tier], 0)}"
            for tier in sorted(TIER_NAMES, reverse=True)
        )
        lines.append(f"{provider}\t{counts}")
    share = intakes["share"] * 100
    lines.append(
        f"operator intakes\t{intakes['at_required_tier']}/{intakes['in_window']}"
        + f" ({share:.0f}%) reach tier {report['required_tier']}"
        + f" over {report['window_days']} days"
    )
    for code in sorted(intakes["refusals"]):
        lines.append(f"refused {code}\t{intakes['refusals'][code]} intakes")
    target = report["acceptance"]["target_share"] * 100
    verdict = "met" if report["acceptance"]["met"] else "not met"
    lines.append(f"acceptance\t{verdict} (target {target:.0f}%)")
    unlocated = report["sessions_without_recorded_transcript"]
    if not unlocated:
        lines.append("transcripts\tevery event records a transcript that still exists")
    discoverable = [row for row in unlocated if row["discoverable"]]
    lines.append(
        f"unrecorded evidence\t{len(discoverable)} of {len(unlocated)} sessions have"
        + " a provider transcript on this machine their events never recorded"
    )
    for row in unlocated:
        found = row["discoverable"] or "no provider file on this machine"
        lines.append(
            f"no transcript\t{row['thread']} ({row['events']} events) — {found}"
        )
    named = report.get("named_thread") or {}
    if named:
        event = named.get("event") or {}
        if event:
            lines.append(
                f"session\t{named['thread']} newest event tier {event['tier']}"
                + f" ({event['tier_name']})"
            )
        else:
            lines.append(f"session\t{named['thread']} has no source event on this machine")
        for row in named.get("intakes", []):
            state = "allowed" if row["allowed"] else str(row["refusal"].get("code", ""))
            lines.append(
                f"session intake\t{row['msg_id']} {state}"
                + f" (chain {row['chain_length']}, min tier {row['min_tier_in_chain']})"
            )
    else:
        newest = report.get("newest_source_event") or {}
        if newest:
            lines.append(
                f"newest event\t{newest['thread']} tier {newest['tier']}"
                + f" ({newest['tier_name']})"
            )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="session-kit doctor --authority",
        description="Report the evidence behind source authority. Records nothing.",
    )
    parser.add_argument("--state-dir", default="")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument("--session", default="", metavar="PROVIDER:UUID")
    parser.add_argument("--action", default="delegate")
    options = parser.parse_args(list(argv) if argv is not None else None)
    if options.days < 1:
        parser.error("--days must be at least 1")
    state = options.state_dir or os.environ.get("SESSION_KIT_STATE_DIR") or os.fspath(
        Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
        / "session-kit"
    )
    report = authority_report(
        state,
        window_days=options.days,
        thread=options.session,
        action=options.action,
    )
    if options.json:
        json.dump(report, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render(report) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
