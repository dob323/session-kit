"""Inspect and resolve no-session prompt handoffs without provider replay."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from typing import Any


MAX_PROMPT_BYTES = 1024 * 1024
UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
SHA_RE = re.compile(r"[0-9a-f]{64}")
KEY_RE = re.compile(r"[0-9a-f]{12}")
ITEM_RE = re.compile(r".+\.prompt\.(?:intake_pending|quarantined)")
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class QuarantineError(ValueError):
    pass


@dataclass(frozen=True)
class Item:
    name: str
    device: int
    inode: int
    modified_ns: int


def _root() -> Path:
    return Path(
        os.environ.get(
            "SESSION_KIT_START_DIR",
            Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
            / "shpool-start",
        )
    )


def _parts(path: Path) -> tuple[str, ...]:
    if not path.is_absolute() or ".." in path.parts:
        raise QuarantineError("prompt quarantine path must be absolute and contained")
    return tuple(part for part in path.parts if part not in {"/", ""})


def _open_directory(path: Path, *, missing_ok: bool = False, private: bool = False) -> int | None:
    """Open every ancestor independently, refusing links at every level."""
    parts = _parts(path)
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | CLOEXEC)
    try:
        for index, part in enumerate(parts):
            try:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW | CLOEXEC,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                if missing_ok and index == len(parts) - 1:
                    os.close(descriptor)
                    return None
                raise
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise QuarantineError("prompt quarantine ancestor is not a directory")
        if private and (
            info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise QuarantineError("prompt quarantine directory is unsafe")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _recheck_directory(path: Path, expected: tuple[int, int], *, private: bool = False) -> None:
    descriptor = _open_directory(path, private=private)
    assert descriptor is not None
    try:
        if _identity(os.fstat(descriptor)) != expected:
            raise QuarantineError("prompt quarantine directory changed during action")
    finally:
        os.close(descriptor)


def _safe_name(name: str) -> None:
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        raise QuarantineError("unsafe prompt quarantine filename")


def _read_file_at(
    directory: int,
    name: str,
    *,
    maximum: int = MAX_PROMPT_BYTES,
    expected: tuple[int, int] | None = None,
) -> tuple[bytes, os.stat_result]:
    _safe_name(name)
    descriptor = os.open(name, os.O_RDONLY | NOFOLLOW | CLOEXEC, dir_fd=directory)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or not 0 < info.st_size <= maximum
            or (expected is not None and _identity(info) != expected)
        ):
            raise QuarantineError("unsafe prompt quarantine file")
        chunks: list[bytes] = []
        remaining = info.st_size
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (
            len(raw) != info.st_size
            or _identity(current) != _identity(info)
            or current.st_size != info.st_size
        ):
            raise QuarantineError("prompt quarantine changed while reading")
        return raw, info
    finally:
        os.close(descriptor)


def _json_at(directory: int, name: str) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, member in pairs:
            if key in value:
                raise QuarantineError("duplicate marker key")
            value[key] = member
        return value

    try:
        raw, _ = _read_file_at(directory, name, maximum=65536)
        value = json.loads(raw, object_pairs_hook=unique)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise QuarantineError("invalid prompt marker") from exc
    if not isinstance(value, dict):
        raise QuarantineError("invalid prompt marker")
    return value


def _items_at(directory: int) -> list[Item]:
    values: list[Item] = []
    for name in os.listdir(directory):
        if ITEM_RE.fullmatch(name) is None:
            continue
        _, info = _read_file_at(directory, name)
        values.append(Item(name, info.st_dev, info.st_ino, info.st_mtime_ns))
    return sorted(values, key=lambda item: (item.modified_ns, item.name))


def _items(root: Path) -> list[Item]:
    directory = _open_directory(root, missing_ok=True, private=True)
    if directory is None:
        return []
    try:
        return _items_at(directory)
    finally:
        os.close(directory)


def _key(value: Item | Path | str) -> str:
    name = value.name if isinstance(value, (Item, Path)) else value
    return hashlib.sha256(name.encode()).hexdigest()[:12]


def _select(root: Path, key: str) -> tuple[int, tuple[int, int], Item]:
    if KEY_RE.fullmatch(key) is None:
        raise QuarantineError("prompt quarantine selection is invalid")
    directory = _open_directory(root, private=True)
    assert directory is not None
    try:
        root_identity = _identity(os.fstat(directory))
        matches = [item for item in _items_at(directory) if _key(item) == key]
        if len(matches) != 1:
            raise QuarantineError("prompt quarantine selection is missing or ambiguous")
        item = matches[0]
        _read_file_at(directory, item.name, expected=(item.device, item.inode))
        return directory, root_identity, item
    except BaseException:
        os.close(directory)
        raise


def _base_name(item: Item) -> str:
    suffix = ".intake_pending" if item.name.endswith(".intake_pending") else ".quarantined"
    return item.name[: -len(suffix)]


def _acceptance(directory: int, item: Item, prompt: bytes) -> dict[str, Any]:
    record = _json_at(directory, f"{_base_name(item)}.accepted")
    if (
        set(record) != {"bytes", "schema_version", "session_id", "sha256", "status", "turn_id"}
        or record["schema_version"] != 1
        or record["status"] != "accepted"
        or record["bytes"] != len(prompt)
        or record["sha256"] != hashlib.sha256(prompt).hexdigest()
        or not isinstance(record["session_id"], str)
        or UUID_RE.fullmatch(record["session_id"]) is None
        or not isinstance(record["turn_id"], str)
        or UUID_RE.fullmatch(record["turn_id"]) is None
    ):
        raise QuarantineError("prompt has no exact provider acceptance proof")
    return record


def _retained_directory(directory: int, *, create: bool) -> int | None:
    name = "prompt-quarantine-retained"
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=directory)
        except FileExistsError:
            pass
    try:
        retained = os.open(name, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW | CLOEXEC, dir_fd=directory)
    except FileNotFoundError:
        return None
    info = os.fstat(retained)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        os.close(retained)
        raise QuarantineError("prompt quarantine retention directory is unsafe")
    return retained


def _archive(directory: int, item: Item, outcome: str) -> None:
    _, source = _read_file_at(
        directory, item.name, expected=(item.device, item.inode)
    )
    retained = _retained_directory(directory, create=True)
    assert retained is not None
    destination = f"{time.time_ns()}-{_key(item)}.{outcome}"
    try:
        try:
            os.stat(destination, dir_fd=retained, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise QuarantineError("prompt quarantine archive destination exists")
        os.rename(item.name, destination, src_dir_fd=directory, dst_dir_fd=retained)
        moved = os.stat(destination, dir_fd=retained, follow_symlinks=False)
        if _identity(moved) != _identity(source):
            raise QuarantineError("prompt quarantine archive identity changed")
        os.fsync(directory)
        os.fsync(retained)
    finally:
        os.close(retained)


def _age_text(seconds: int) -> str:
    if seconds < 120:
        return f"{seconds}s old"
    if seconds < 7200:
        return f"{seconds // 60}m old"
    return f"{seconds // 3600}h old"


def item_records(root: Path) -> list[dict[str, Any]]:
    now_ns = time.time_ns()
    records = []
    for item in _items(root):
        records.append(
            {
                "age_seconds": max(0, (now_ns - item.modified_ns) // 1_000_000_000),
                "key": _key(item),
                "state": "intake_pending" if item.name.endswith(".intake_pending") else "outcome_unknown",
                "title": "Codex prompt intake pending" if item.name.endswith(".intake_pending") else "Codex prompt outcome unknown",
            }
        )
    return records


def list_items(root: Path, *, json_mode: bool = False) -> int:
    records = item_records(root)
    if json_mode:
        print(json.dumps({"items": records, "schema_version": 1}, sort_keys=True, separators=(",", ":")))
        return 0
    for record in records:
        print(f"Needs You  {record['title']}  {_age_text(record['age_seconds'])}")
    return 0


def ingest(root: Path, key: str) -> int:
    directory, root_identity, item = _select(root, key)
    try:
        if not item.name.endswith(".intake_pending"):
            raise QuarantineError("only a provider-accepted intake-pending prompt can be ingested")
        prompt, _ = _read_file_at(directory, item.name, expected=(item.device, item.inode))
        accepted = _acceptance(directory, item, prompt)
        completion = _json_at(directory, f"{_base_name(item)}.completed")
        generation = completion.get("managed_generation")
        if (
            completion.get("schema_version") != 3
            or completion.get("status") != "intake_pending"
            or not isinstance(generation, str)
            or not generation
        ):
            raise QuarantineError("intake-pending prompt has no exact managed generation")
        payload = {
            "hook_event_name": "UserPromptSubmit",
            "prompt": prompt.decode("utf-8"),
            "session_id": accepted["session_id"],
            "turn_id": accepted["turn_id"],
        }
        base = root / _base_name(item)
        environment = os.environ.copy()
        environment.update(
            {
                "SESSION_KIT_SOURCE_ACCEPTANCE_PATH": f"{base}.source_accepted",
                "SESSION_KIT_SOURCE_ACCEPTANCE_DIGEST": hashlib.sha256(prompt).hexdigest(),
                "SESSION_KIT_INTAKE_COMMIT_PATH": f"{base}.intake_committed",
                "SESSION_KIT_MANAGED_GENERATION": generation,
            }
        )
        hook = Path(__file__).resolve().parents[2] / "extras/hooks/sk_codex_intake.py"
        result = subprocess.run(
            [sys.executable, os.fspath(hook)],
            input=json.dumps(payload).encode(),
            env=environment,
            timeout=5,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _recheck_directory(root, root_identity, private=True)
        if result.returncode != 0:
            raise QuarantineError("supervisor intake did not produce a durable commit; prompt retained")
        record = _json_at(directory, f"{_base_name(item)}.intake_committed")
        if (
            record.get("schema_version") != 2
            or record.get("status") != "intake_committed"
            or record.get("session_id") != accepted["session_id"]
            or record.get("submission_key") != accepted["turn_id"]
            or record.get("prompt_sha256") != hashlib.sha256(prompt).hexdigest()
            or not isinstance(record.get("source_event_id"), str)
            or SHA_RE.fullmatch(record["source_event_id"]) is None
        ):
            raise QuarantineError("supervisor intake commit does not match the quarantined prompt")
        _archive(directory, item, "ingested")
    finally:
        os.close(directory)
    print("Prompt was ingested by the supervisor without provider replay.")
    return 0


def discard(root: Path, key: str) -> int:
    directory, root_identity, item = _select(root, key)
    try:
        _recheck_directory(root, root_identity, private=True)
        _archive(directory, item, "discarded")
    finally:
        os.close(directory)
    print("Prompt was discarded from Needs You and retained for 30-day recovery.")
    return 0


def resume(root: Path, key: str, cwd: Path) -> int:
    directory, root_identity, item = _select(root, key)
    try:
        prompt, _ = _read_file_at(directory, item.name, expected=(item.device, item.inode))
        accepted = _acceptance(directory, item, prompt)
        cwd_descriptor = _open_directory(cwd)
        if cwd_descriptor is None:
            raise QuarantineError("resume requires an existing absolute project directory")
        cwd_identity = _identity(os.fstat(cwd_descriptor))
        os.close(cwd_descriptor)
        sp = Path(__file__).resolve().parents[2] / "bin/sp"
        result = subprocess.run(
            [os.fspath(sp), "restore-exact", "codex", accepted["session_id"], os.fspath(cwd)],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        _recheck_directory(root, root_identity, private=True)
        _recheck_directory(cwd, cwd_identity)
        if result.returncode != 0:
            raise QuarantineError("exact conversation resume was refused; prompt retained")
        _archive(directory, item, "resumed")
    finally:
        os.close(directory)
    print("Exact Codex conversation was resumed in a managed session.")
    return 0


def prune(root: Path) -> int:
    directory = _open_directory(root, missing_ok=True, private=True)
    if directory is None:
        print("Pruned 0 expired prompt quarantine record(s).")
        return 0
    root_identity = _identity(os.fstat(directory))
    retained = None
    try:
        retained = _retained_directory(directory, create=False)
        if retained is None:
            print("Pruned 0 expired prompt quarantine record(s).")
            return 0
        cutoff_ns = time.time_ns() - 30 * 86400 * 1_000_000_000
        removed = 0
        for name in os.listdir(retained):
            raw, info = _read_file_at(retained, name)
            del raw
            if info.st_mtime_ns >= cutoff_ns:
                continue
            current = os.stat(name, dir_fd=retained, follow_symlinks=False)
            if _identity(current) != _identity(info):
                raise QuarantineError("prompt quarantine retention record changed")
            os.unlink(name, dir_fd=retained)
            removed += 1
        _recheck_directory(root, root_identity, private=True)
        os.fsync(retained)
    finally:
        if retained is not None:
            os.close(retained)
        os.close(directory)
    print(f"Pruned {removed} expired prompt quarantine record(s).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    command = sub.add_parser("list")
    command.add_argument("--json", action="store_true")
    for action in ("ingest", "discard"):
        command = sub.add_parser(action)
        command.add_argument("key")
    command = sub.add_parser("resume")
    command.add_argument("key")
    command.add_argument("cwd", type=Path)
    sub.add_parser("prune")
    args = parser.parse_args(argv)
    root = _root()
    try:
        if args.action == "list":
            return list_items(root, json_mode=args.json)
        if args.action == "ingest":
            return ingest(root, args.key)
        if args.action == "discard":
            return discard(root, args.key)
        if args.action == "resume":
            return resume(root, args.key, args.cwd)
        return prune(root)
    except (OSError, UnicodeError, QuarantineError, subprocess.TimeoutExpired) as exc:
        print(f"session-kit: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
