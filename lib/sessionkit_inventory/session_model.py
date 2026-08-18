"""Which model a session is running RIGHT NOW, and its name for a person.

The launch argument is not the answer. `--model` says what a session was
started with; a person who types `/model` inside the session changes what it
actually runs and never touches the command line again. A picker that reads
argv therefore shows the wrong model for the rest of that session's life, and
says it with total confidence.

The conversation's own record is the answer. Claude writes `message.model` on
every assistant record it appends; Codex writes `turn_context.model` at the
start of every turn and a `thread_settings_applied` event when the setting
changes. Both are appended by the provider itself, both move when the person
moves the model, and the LAST one in the file is what the session is on.

Reading that cheaply is the whole problem. Transcripts here run to 34 MB, and
a tail is not enough: one live transcript on this machine has 45,019
`agent-color` records after its last assistant record, pushing the model
4.2 MB back from the end. So the read is done once and then never repeated:
a small cache per conversation remembers the offset already scanned, and every
later refresh reads only the bytes the provider has appended since. Steady
state is one stat plus a few kilobytes; a first sighting is one bounded
backward scan.

Nothing here is required for a row to render. Every failure path returns an
empty string, and the caller says so in the cell rather than guessing.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import time
from typing import Any, Iterable, Mapping

from .common import clean_text


SCHEMA_VERSION = 1
CACHE_DIRECTORY = "model"

# The first backward window, per provider, chosen from measurement rather than
# taste, and the two providers are nothing alike.
#
# Claude appends `message.model` on every assistant record, so the newest one is
# usually near the end: 11 of 16 live transcripts here carry it within 48 KB.
#
# Codex writes `turn_context` ONCE at the start of a turn and then hundreds of
# kilobytes of events behind it, so the newest model record is always far back.
# Measured on the 12 newest live rollouts: **12 of 12** sit beyond 64 KB, from
# 249 KB to 1.43 MB. Starting Codex at 64 KB meant every first sighting paid
# 64 KB + 512 KB + 4 MB of escalating reads to find something a single 2 MB
# read finds outright.
FIRST_SCAN_BYTES = {"claude": 64 * 1024, "codex": 2 * 1024 * 1024}
DEFAULT_FIRST_SCAN_BYTES = 64 * 1024
# The ceiling on one backward scan. The worst live case measured was 4.25 MB;
# this is four times that, and a file whose model sits further back reads as
# unknown rather than as a slow picker.
MAX_SCAN_BYTES = 16 * 1024 * 1024

# How long a "this machine has no record of that conversation" answer is
# trusted before looking again. Short enough that a session whose transcript
# has just appeared is picked up while a person is still reading the screen.
MISS_RETRY_SECONDS = 15.0

# The byte a candidate line must contain before it is worth parsing. Both
# providers spell the key the same way, and the escaped form a tool argument
# would produce (`\"model\"`) does not match.
MODEL_MARKER = b'"model"'

CODEX_TURN_MARKER = b'"turn_context"'
CODEX_SETTINGS_MARKER = b'"thread_settings_applied"'

SOURCE_TRANSCRIPT = "transcript"
SOURCE_LAUNCH = "launch argument"


# --------------------------------------------------------------- human names

CLAUDE_FAMILY_NAMES = {
    "opus": "Opus",
    "sonnet": "Sonnet",
    "haiku": "Haiku",
    "fable": "Fable",
}

# The non-numeric words a Codex identifier may carry, and how the vendor writes
# each of them. A token that is not here means this rule does not understand the
# identifier, and an identifier it does not understand keeps its raw form.
CODEX_KNOWN_TOKENS = {
    "codex": "Codex",
    "sol": "Sol",
    "max": "Max",
    "mini": "Mini",
    "nano": "Nano",
    "preview": "Preview",
    "turbo": "Turbo",
    "pro": "Pro",
}


def _is_release_date(token: str) -> bool:
    """`20250929` in `claude-sonnet-4-5-20250929` — a build stamp, not a name."""
    return len(token) == 8 and token.isdigit()


def _claude_name(model_id: str) -> str:
    """`claude-sonnet-4-5-20250929` becomes `Sonnet 4.5`.

    Every token must be accounted for. This used to skip whatever it did not
    recognise, so `claude-opus-experimental-5` rendered as `Opus 5` — a
    different model's name, indistinguishable on screen from the real one.
    A name is only produced when the identifier is fully understood; anything
    else keeps its raw identifier, which is ugly and true.
    """
    body = model_id[len("claude-") :] if model_id.startswith("claude-") else model_id
    family = ""
    version: list[str] = []
    for token in body.split("-"):
        if not token or _is_release_date(token):
            continue
        folded = token.casefold()
        if not family and folded in CLAUDE_FAMILY_NAMES:
            family = CLAUDE_FAMILY_NAMES[folded]
        elif token.replace(".", "").isdigit():
            version.append(token)
        else:
            return ""
    if not family:
        return ""
    return f"{family} {'.'.join(version)}".strip()


def _codex_name(model_id: str) -> str:
    """`gpt-5-codex` becomes `GPT-5-Codex`.

    Derived rather than looked up. Codex caches the vendor's own display names
    in `models_cache.json`, but that file is 346 KB on this machine and this
    rule reproduces its `display_name` exactly for the models actually in use,
    so a third of a megabyte is not read on every refresh to learn nothing.

    Every token must be one this rule recognises. Title-casing an unfamiliar
    identifier produced a confident-looking name for a model nobody had heard
    of, which reads exactly like an authoritative one.
    """
    parts: list[str] = []
    for token in model_id.split("-"):
        if not token:
            continue
        folded = token.casefold()
        if folded == "gpt" or (
            folded.startswith("gpt") and token[3:].replace(".", "").isdigit()
        ):
            parts.append("GPT" + token[3:])
        elif folded in {"o1", "o3", "o4"}:
            parts.append(token.upper())
        elif token.replace(".", "").isdigit():
            parts.append(token)
        elif folded in CODEX_KNOWN_TOKENS:
            parts.append(CODEX_KNOWN_TOKENS[folded])
        else:
            return ""
    return "-".join(parts)


def human_model_name(provider: object, model_id: object) -> str:
    """The model's product name, or the raw identifier when it has no rule."""
    text = clean_text(model_id, 80)
    if not text:
        return ""
    family = clean_text(provider, 20).casefold()
    if family == "claude" or text.casefold().startswith("claude-"):
        return _claude_name(text) or text
    if family == "codex" or text.casefold().startswith(("gpt", "o1", "o3", "o4")):
        return _codex_name(text) or text
    return text


# ------------------------------------------------------------- record readers


def _claude_model_from_line(line: bytes) -> str:
    try:
        record = json.loads(line)
    except (UnicodeDecodeError, ValueError):
        return ""
    if not isinstance(record, Mapping):
        return ""
    message = record.get("message")
    if not isinstance(message, Mapping):
        return ""
    model = clean_text(message.get("model"), 80)
    # Claude writes assistant records it generated itself -- an API failure
    # notice, a cancelled turn -- with the literal model `<synthetic>`. Read
    # naively, one of those at the end of a transcript renames the session's
    # model to `<synthetic>` on the picker. Measured on a live transcript here.
    if model.startswith("<"):
        return ""
    return model


def _codex_model_from_line(line: bytes) -> str:
    try:
        record = json.loads(line)
    except (UnicodeDecodeError, ValueError):
        return ""
    if not isinstance(record, Mapping):
        return ""
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        return ""
    if record.get("type") == "turn_context":
        return clean_text(payload.get("model"), 80)
    if payload.get("type") == "thread_settings_applied":
        settings = payload.get("thread_settings")
        if isinstance(settings, Mapping):
            return clean_text(settings.get("model"), 80)
    return ""


def _model_from_line(provider: str, line: bytes) -> str:
    if provider == "claude":
        return _claude_model_from_line(line)
    if provider == "codex":
        return _codex_model_from_line(line)
    return ""


def _interesting(provider: str, line: bytes) -> bool:
    if MODEL_MARKER not in line:
        return False
    if provider == "codex":
        return CODEX_TURN_MARKER in line or CODEX_SETTINGS_MARKER in line
    return True


def newest_model(provider: str, block: bytes) -> str:
    """The last model recorded in one whole-line block, or ''."""
    for line in reversed(block.split(b"\n")):
        if not line or not _interesting(provider, line):
            continue
        found = _model_from_line(provider, line)
        if found:
            return found
    return ""


def _read_aligned(
    descriptor: int, start: int, size: int, *, aligned: bool
) -> tuple[bytes, int]:
    """Whole lines from ``start`` to ``size``, and the offset they end on.

    A block that begins mid-record would be parsed as garbage, so a start the
    caller cannot vouch for drops its first partial line. ``aligned`` says the
    caller already knows this offset is a line boundary — a resumed read starts
    exactly where the last one stopped, and dropping a line there would skip a
    whole record the provider had just appended.

    The returned offset is the boundary after the last complete line, and it is
    absolute: it is what the next resumed read starts from.
    """
    length = max(0, size - start)
    if length <= 0:
        return b"", start
    try:
        raw = os.pread(descriptor, length, start)
    except OSError:
        return b"", start
    if not raw:
        return b"", start
    if start and not aligned:
        boundary = raw.find(b"\n")
        if boundary < 0:
            return b"", start
        start += boundary + 1
        raw = raw[boundary + 1 :]
    end = raw.rfind(b"\n")
    if end < 0:
        return b"", start
    return raw[: end + 1], start + end + 1


def scan_backward(descriptor: int, provider: str, size: int) -> tuple[str, int]:
    """The newest model in a bounded window at the end of the file.

    Returns the model and the offset scanned to, so the caller can record how
    much of the file it has already accounted for.
    """
    window = FIRST_SCAN_BYTES.get(provider, DEFAULT_FIRST_SCAN_BYTES)
    scanned_to = 0
    while True:
        start = max(0, size - window)
        block, end_offset = _read_aligned(descriptor, start, size, aligned=start == 0)
        if block:
            scanned_to = end_offset
            found = newest_model(provider, block)
            if found:
                return found, scanned_to
        if start == 0 or window >= MAX_SCAN_BYTES:
            # `scanned_to` stays 0 when the file held no complete line yet --
            # a first assistant record caught mid-write. Claiming the file size
            # instead parked the resume point in the middle of that record, and
            # once it completed the reader started reading from its middle and
            # could never parse it: the conversation said `no reply yet`
            # forever, but only with a cache. Nothing scanned, nothing claimed.
            return "", scanned_to
        window = min(window * 8, MAX_SCAN_BYTES)


# -------------------------------------------------------------------- cache


def prune_cache(
    cache_dir: object,
    live_keys: Iterable[object] | None,
    *,
    now: float | None = None,
) -> int:
    """Drop cache records for conversations that are gone.

    Everything else under the state directory has a reaper; this did not, and
    it writes one small file per conversation the kit has ever read a model for
    — including a `missed_at` marker for every session whose transcript could
    not be found. Small each, unbounded together, over the life of an install.

    A record is kept while its conversation is live, and for a week after it
    stops appearing: a closed conversation can be restored, and re-reading a
    34 MB transcript from scratch is the cost of forgetting too eagerly.
    """
    root = Path(str(cache_dir)) / CACHE_DIRECTORY if cache_dir else None
    if root is None or not root.is_dir():
        return 0
    keep = set()
    for provider in ("claude", "codex"):
        for key in live_keys or ():
            exact = clean_text(key, 64)
            if exact:
                keep.add(cache_path(Path(str(cache_dir)), provider, exact).name)
    moment = time.time() if now is None else now
    retain_seconds = 7 * 86400
    removed = 0
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0
    for path in entries:
        if path.name in keep or not path.name.endswith(".json"):
            continue
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                continue
            if moment - info.st_mtime < retain_seconds:
                continue
            path.unlink()
        except OSError:
            continue
        removed += 1
    return removed


def cache_path(cache_dir: Path, provider: str, key: str) -> Path:
    digest = hashlib.blake2b(
        f"{provider}\0{key}".encode("utf-8", "replace"), digest_size=16
    ).hexdigest()
    return Path(cache_dir) / CACHE_DIRECTORY / f"{digest}.json"


def _read_cache(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(record, Mapping):
        return None
    if record.get("schema_version") != SCHEMA_VERSION:
        return None
    missed_at = record.get("missed_at")
    if isinstance(missed_at, (int, float)) and not isinstance(missed_at, bool):
        return {"missed_at": float(missed_at)}
    for name in ("inode", "device", "scanned_to", "ctime_ns"):
        value = record.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
    model = record.get("model")
    transcript = record.get("transcript")
    if not isinstance(model, str) or not isinstance(transcript, str):
        return None
    if not transcript.startswith("/"):
        return None
    return {
        "inode": record["inode"],
        "device": record["device"],
        "ctime_ns": record["ctime_ns"],
        "scanned_to": record["scanned_to"],
        "model": clean_text(model, 80),
        "transcript": transcript,
    }


def _write_cache(path: Path, record: Mapping[str, Any]) -> None:
    """Best effort. A cache that cannot be written costs speed, never truth."""
    if "missed_at" in record:
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "missed_at": float(record["missed_at"]),
        }
    else:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "inode": int(record["inode"]),
            "device": int(record["device"]),
            "ctime_ns": int(record["ctime_ns"]),
            "scanned_to": int(record["scanned_to"]),
            "model": clean_text(record.get("model"), 80),
            "transcript": str(record["transcript"]),
        }
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    except OSError:
        try:
            os.unlink(temporary)  # type: ignore[possibly-undefined]
        except (OSError, NameError, UnboundLocalError):
            pass


def _open(path: object) -> tuple[int, Any] | None:
    if not path:
        return None
    try:
        descriptor = os.open(Path(str(path)), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        return descriptor, os.fstat(descriptor)
    except OSError:
        os.close(descriptor)
        return None


def current_model(
    provider: object,
    *,
    key: str,
    locate: Any,
    cache_dir: object = None,
) -> tuple[str, bool]:
    """The model this conversation runs now, and whether its record was found.

    ``locate`` is called only when the cache cannot answer where the record
    is. That matters more than it looks: finding a Codex rollout means walking
    a sessions tree of several hundred files, and finding a Claude transcript
    means globbing every profile root, so doing it per session per refresh is
    the expensive part of this — not the reading.

    ``cache_dir`` is what makes the reading cheap: with one, a conversation is
    scanned backward once and afterwards only the bytes the provider has
    appended since are read. Without one, every call is a bounded backward
    scan, which is correct and slower.
    """
    family = clean_text(provider, 20).casefold()
    if family not in {"claude", "codex"}:
        return "", False
    cache_file = None
    record = None
    if cache_dir and key:
        cache_file = cache_path(Path(str(cache_dir)), family, key)
        record = _read_cache(cache_file)
    if record is not None and "missed_at" in record:
        # A conversation with no record on this disk. Looking for one means
        # globbing every Claude profile root or walking a Codex sessions tree
        # of several hundred files -- measured at 12 ms, on every refresh, for
        # a row that is going to say the same thing each time. Remember the
        # miss briefly instead; a session whose transcript appears gets picked
        # up on the next look, seconds later.
        if 0 <= time.time() - record["missed_at"] < MISS_RETRY_SECONDS:
            return "", False
        record = None
    opened = None
    resumable = False
    if record is not None:
        opened = _open(record["transcript"])
        if opened is not None:
            info = opened[1]
            # The inode-change time is part of the identity because an inode
            # is NOT: a transcript deleted and recreated at the same path is
            # routinely handed the same inode number, and (inode, device)
            # alone accepted the new file as the old one and answered from the
            # cache -- a model name for a file that no longer exists, printed
            # as fact. Measured on this filesystem, not imagined.
            resumable = (
                record["inode"] == info.st_ino
                and record["device"] == info.st_dev
                and record["ctime_ns"] == info.st_ctime_ns
                and record["scanned_to"] <= info.st_size
            )
            if not resumable:
                os.close(opened[0])
                opened = None
    transcript = record["transcript"] if resumable and record else None
    if opened is None:
        transcript = locate() if callable(locate) else locate
        opened = _open(transcript)
    if opened is None:
        if cache_file is not None:
            _write_cache(cache_file, {"missed_at": time.time()})
        return "", False
    descriptor, info = opened
    try:
        size = info.st_size
        if size <= 0:
            return "", True
        if resumable and record is not None:
            if record["scanned_to"] == size:
                return record["model"], True
            block, end_offset = _read_aligned(
                descriptor, record["scanned_to"], size, aligned=True
            )
            model = record["model"]
            scanned_to = record["scanned_to"]
            if block:
                scanned_to = end_offset
                found = newest_model(family, block)
                if found:
                    model = found
        else:
            model, scanned_to = scan_backward(descriptor, family, size)
        if cache_file is not None and transcript:
            _write_cache(
                cache_file,
                {
                    "inode": info.st_ino,
                    "device": info.st_dev,
                    "ctime_ns": info.st_ctime_ns,
                    "scanned_to": scanned_to,
                    "model": model,
                    "transcript": os.fspath(transcript),
                },
            )
        return model, True
    finally:
        os.close(descriptor)
