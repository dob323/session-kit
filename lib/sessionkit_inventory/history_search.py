"""Search recorded history without exposing its storage layout."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import gzip
import json
from pathlib import Path
import re
import unicodedata
from typing import Iterable, Mapping

MAX_SHOWN_LINES = 8
MAX_MATCH_BYTES = 512
MAX_CAPTURE_CHARS = 2048
MAX_METADATA_LINES = 4096
ARCHIVE_ID = re.compile(r"^\d{8}-\d{6}-(.+?)\.raw\.gz$")
RECORDING_DATE = re.compile(r"(?:^|[^0-9])(\d{8})-(\d{6})(?:[^0-9]|$)")
SCRIPT_DATE = re.compile(r"Script started on (\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})")
ANSI_SEQUENCE = re.compile(
    r"\x1b(?:"
    r"\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]"
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|[PX\^_][^\x1b\x07]*(?:\x07|\x1b\\)"
    r"|[\x20-\x2f]+[\x30-\x7e]"
    r"|[\x30-\x4f\x51-\x57\x59\x5a\x5c\x60-\x7e]"
    r")"
)


@dataclass(frozen=True)
class RecordingText:
    count: int
    shown: list[str]
    prompt: str = ""
    started: str = ""


def _clean(value: object, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    return "".join(ch for ch in text if ch >= " " and ch != "\x7f")[:limit]


def _read_json(path: Path) -> object:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def session_names(
    inventory: Path | None, recovery: Path, data: Path
) -> dict[tuple[str, str], str]:
    """Names keyed by both store and session identity.

    Shpool names are reused.  In particular, a live ``main2`` title says
    nothing about an older ``main2`` archive, so title provenance is part of
    the key rather than one global id-to-name table.
    """
    names: dict[tuple[str, str], str] = {}
    document = _read_json(inventory) if inventory else {}
    if isinstance(document, Mapping):
        for row in document.get("sessions") or ():
            if not isinstance(row, Mapping):
                continue
            session_id = row.get("shpool_id_raw")
            title = _clean(row.get("title"))
            if isinstance(session_id, str) and session_id and title:
                names[("journal", session_id)] = title
    manifest = _read_json(recovery / "recovery-manifest.json")
    rows = manifest.get("sessions") if isinstance(manifest, Mapping) else None
    if isinstance(rows, Mapping):
        manifest_rows = rows.items()
    else:
        manifest_rows = (("", row) for row in (rows or ()))
    for manifest_id, row in manifest_rows:
        if not isinstance(row, Mapping):
            continue
        session_id = (
            row.get("shpool_id") or row.get("shpool_id_raw") or manifest_id
        )
        title = _clean(row.get("title"))
        if isinstance(session_id, str) and session_id and title:
            names.setdefault(("recovery", session_id), title)
    # Closed-session ledger rows have a reusable shpool id but no recording
    # identity, so they cannot safely name archives.  An archive is named only
    # by metadata inside that recording, then its own date and first prompt.
    return names


def _source_id(path: Path, journal: Path, recovery: Path, archive: Path) -> str:
    if journal in path.parents and path.name.endswith(".rendered.txt"):
        return path.name.removesuffix(".rendered.txt")
    if journal in path.parents:
        relative = path.relative_to(journal)
        return relative.parts[0].removesuffix(".raw")
    if recovery in path.parents:
        try:
            for line in (recovery / "current-map.tsv").read_text(
                encoding="utf-8"
            ).splitlines():
                session_id, recorded = line.split("\t", 1)
                if Path(recorded) == path:
                    return session_id
        except (OSError, ValueError):
            pass
    if archive in path.parents:
        match = ARCHIVE_ID.match(path.name)
        return match.group(1) if match else ""
    return path.stem.removesuffix(".raw")


def _source_store(path: Path, journal: Path, recovery: Path, archive: Path) -> str:
    if journal == path.parent or journal in path.parents:
        return "journal"
    if recovery == path.parent or recovery in path.parents:
        return "recovery"
    if archive == path.parent or archive in path.parents:
        return "archive"
    return "recording"


def _recording_key(
    path: Path, journal: Path, recovery: Path, archive: Path
) -> tuple[str, str]:
    """A storage-private identity; it is deliberately never rendered."""
    store = _source_store(path, journal, recovery, archive)
    if store == "journal":
        relative = path.relative_to(journal)
        if relative.name == "rendered.txt":
            return store, f"directory:{relative.parent.as_posix()}"
        if relative.name.endswith(".rendered.txt"):
            return store, f"sidecar:{relative.as_posix()}"
        if len(relative.parts) > 1:
            return store, f"directory:{relative.parent.as_posix()}"
        return store, f"raw:{relative.name}"
    # Recovery and archive files are snapshots.  A recycled shpool id must not
    # merge two independently recorded snapshots.
    root = recovery if store == "recovery" else archive
    try:
        return store, path.relative_to(root).as_posix()
    except ValueError:
        return store, path.name


def sources(journal: Path, recovery: Path, archive: Path) -> Iterable[Path]:
    rendered = sorted(journal.glob("**/rendered.txt"))
    rendered += sorted(journal.glob("**/*.rendered.txt"))
    rendered = list(dict.fromkeys(rendered))
    yield from rendered
    for path in sorted(journal.glob("**/*.raw")):
        sidecar = path.with_suffix(".rendered.txt")
        directory_sidecar = path.parent / "rendered.txt"
        if sidecar.is_file() or directory_sidecar.is_file():
            continue
        yield path
    yield from sorted(recovery.glob("**/*.raw"))
    yield from sorted(archive.glob("**/*.gz"))


def _safe_line(value: str, limit: int = MAX_MATCH_BYTES) -> str:
    text = ANSI_SEQUENCE.sub("", value.rstrip("\r\n"))
    text = "".join(
        character
        for character in text
        if not unicodedata.category(character).startswith("C")
    )
    payload = text.encode("utf-8")
    if len(payload) > limit:
        text = payload[:limit].decode("utf-8", "ignore")
    return text


def _prompt_from_line(line: str, waiting_for_operator: bool) -> tuple[str, bool]:
    stripped = line.strip()
    if not stripped:
        return "", waiting_for_operator
    if stripped.startswith("══ OPERATOR"):
        return "", True
    if waiting_for_operator:
        return stripped, False
    if stripped.startswith(("❯", "›")):
        prompt = stripped[1:].strip()
        # Startup convenience commands are not the subject of the recording.
        if prompt and not prompt.startswith("/"):
            return prompt, False
    return "", waiting_for_operator


def _matching_lines(path: Path, query: str) -> RecordingText:
    if path.suffix == ".gz":
        handle = gzip.open(path, mode="rt", encoding="utf-8", errors="replace")
    else:
        handle = open(path, encoding="utf-8", errors="replace")
    with handle:
        count = 0
        shown: list[str] = []
        prompt = ""
        started = ""
        waiting_for_operator = False
        for line_number, line in enumerate(handle, start=1):
            if line_number <= MAX_METADATA_LINES and not started:
                match = SCRIPT_DATE.search(line)
                if match:
                    started = f"{match.group(1)} {match.group(2)}"
            if line_number <= MAX_METADATA_LINES and not prompt:
                candidate, waiting_for_operator = _prompt_from_line(
                    line, waiting_for_operator
                )
                if candidate:
                    prompt = candidate[:MAX_MATCH_BYTES]
            if query not in line.casefold():
                continue
            count += 1
            if len(shown) < MAX_SHOWN_LINES:
                # Retain a small amount of slack for output-time sanitation
                # without keeping an arbitrarily large matched line alive.
                shown.append(line[:MAX_CAPTURE_CHARS])
        return RecordingText(count, shown, prompt, started)


def _recording_date(path: Path, text: RecordingText) -> str:
    if text.started:
        return text.started
    for part in reversed(path.parts):
        match = RECORDING_DATE.search(part)
        if match:
            try:
                stamp = datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S")
            except ValueError:
                continue
            return stamp.strftime("%Y-%m-%d %H:%M")
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:
        return "unknown date"


def _recording_label(path: Path, text: RecordingText) -> str:
    label = f"Recorded {_recording_date(path, text)}"
    if text.prompt:
        prompt = _clean(_safe_line(text.prompt, 80), 80)
        if prompt:
            label += f" — {prompt}"
    return label


def render_matches(
    query: str,
    *,
    journal: Path,
    recovery: Path,
    archive: Path,
    inventory: Path | None = None,
    data: Path,
) -> str:
    names = session_names(inventory, recovery, data)
    groups: dict[tuple[str, str], tuple[str, int, list[str]]] = {}
    failures = 0
    for path in sources(journal, recovery, archive):
        try:
            matched = _matching_lines(path, query.casefold())
            session_id = _source_id(path, journal, recovery, archive)
            store = _source_store(path, journal, recovery, archive)
            key = _recording_key(path, journal, recovery, archive)
        except Exception:
            failures += 1
            continue
        if not matched.count:
            continue
        label = names.get((store, session_id), "") or _recording_label(path, matched)
        previous_label, previous_count, previous_lines = groups.get(
            key, (label, 0, [])
        )
        remaining = MAX_SHOWN_LINES - len(previous_lines)
        groups[key] = (
            previous_label,
            previous_count + matched.count,
            previous_lines
            + [_safe_line(line) for line in matched.shown[:remaining]],
        )
    total = sum(count for _label, count, _lines in groups.values())
    if not total:
        out = ["Matches: none."]
        if failures:
            noun = "recording" if failures == 1 else "recordings"
            out.append(f"Notice: {failures} {noun} could not be searched.")
        return "\n".join(out) + "\n"
    out = [f"Matches: {total}"]
    label_totals: dict[str, int] = {}
    for label, _count, _lines in groups.values():
        label_totals[label] = label_totals.get(label, 0) + 1
    label_ordinals: dict[str, int] = {}
    for label, count, lines in groups.values():
        if label_totals[label] > 1:
            ordinal = label_ordinals.get(label, 0) + 1
            label_ordinals[label] = ordinal
            label = f"{label} (recording {ordinal} of {label_totals[label]})"
        noun = "match" if count == 1 else "matches"
        out.extend(("", f"{label} · {count} {noun}"))
        out.extend(lines)
        if count > MAX_SHOWN_LINES:
            out.append(f"  … ({count - MAX_SHOWN_LINES} more matches)")
    if failures:
        noun = "recording" if failures == 1 else "recordings"
        out.extend(("", f"Notice: {failures} {noun} could not be searched."))
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="history_search.py")
    parser.add_argument("query")
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--recovery", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--data", type=Path, required=True)
    args = parser.parse_args(argv)
    print(
        render_matches(
            args.query,
            journal=args.journal,
            recovery=args.recovery,
            archive=args.archive,
            inventory=args.inventory,
            data=args.data,
        ),
        end="",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
