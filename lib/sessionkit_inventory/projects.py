"""Discover and manage Session Kit project shortcuts.

Discovery is deliberately local and bounded. It reads only project paths that
Claude Code or Codex already recorded, then keeps paths that still name an
existing directory. It never searches the rest of the filesystem.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import tempfile
import time
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote

try:
    import tomllib
except ImportError:  # Python 3.10 is supported on Linux.
    tomllib = None  # type: ignore[assignment]


PROVIDERS = ("claude", "codex")
MAX_PROVIDER_CONFIG_BYTES = 32 * 1024 * 1024
MAX_CLAUDE_HISTORY_BYTES = 64 * 1024 * 1024
MAX_PROJECTS_FILE_BYTES = 4 * 1024 * 1024
MAX_PROJECTS = 2048
ALIAS_RE = re.compile(r"[a-z0-9_-]+")
GENERIC_DIRECTORY_NAMES = {
    "app",
    "code",
    "main",
    "project",
    "projects",
    "repo",
    "repository",
    "source",
    "src",
    "v1",
    "v2",
    "v3",
}
CODEX_PROJECT_HEADER_RE = re.compile(
    r'^\s*\[projects\.((?:"(?:[^"\\]|\\.)*")|(?:\'[^\']*\'))\]\s*(?:#.*)?$'
)


class ProjectError(RuntimeError):
    """A provider project record or projects file is unsafe or invalid."""


def _read_owner_file(
    path: Path, *, label: str, max_bytes: int, allow_missing: bool = True
) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        if allow_missing:
            return None
        raise ProjectError(f"{label} does not exist: {path}")
    except OSError as exc:
        raise ProjectError(f"cannot open {label}: {path}") from exc
    try:
        before = os.fstat(descriptor)
        try:
            pathname = path.lstat()
        except OSError as exc:
            raise ProjectError(f"cannot inspect {label}: {path}") from exc
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o022
            or before.st_size < 0
            or before.st_size > max_bytes
            or (before.st_dev, before.st_ino) != (pathname.st_dev, pathname.st_ino)
        ):
            raise ProjectError(f"unsafe {label}: {path}")
        payload = bytearray()
        remaining = before.st_size
        while remaining:
            block = os.read(descriptor, min(remaining, 65536))
            if not block:
                raise ProjectError(f"short read from {label}: {path}")
            payload.extend(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            raise ProjectError(f"{label} grew while being read: {path}")
        after = os.fstat(descriptor)
        stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in stable):
            raise ProjectError(f"{label} changed while being read: {path}")
        return bytes(payload)
    finally:
        os.close(descriptor)


def _directory(raw: object) -> str | None:
    if not isinstance(raw, str) or not raw.startswith("/") or len(raw) > 4096:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        return None
    try:
        resolved = Path(raw).resolve(strict=True)
    except OSError:
        return None
    return os.fspath(resolved) if resolved.is_dir() else None


def _outside_roots(path: str | None, roots: Iterable[Path]) -> str | None:
    if path is None:
        return None
    candidate = Path(path)
    return (
        None
        if any(candidate == root or candidate.is_relative_to(root) for root in roots)
        else path
    )


def _json_object(payload: bytes, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProjectError(f"{label} is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise ProjectError(f"{label} is not a JSON object")
    return value


def _claude_paths(home: Path, warnings: list[str]) -> set[str]:
    paths: set[str] = set()
    excluded = [
        root
        for root in (Path("/tmp"), Path("/private/tmp"))
        if not home.is_relative_to(root)
    ]
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir and (temporary := _directory(tmpdir)):
        temporary_root = Path(temporary)
        if not home.is_relative_to(temporary_root):
            excluded.append(temporary_root)
    settings = home / ".claude" / "settings.json"
    try:
        settings_payload = _read_owner_file(
            settings, label="Claude Code settings", max_bytes=MAX_PROVIDER_CONFIG_BYTES
        )
        if settings_payload is not None:
            environment = _json_object(settings_payload, "Claude Code settings").get(
                "env"
            )
            configured_tmp = (
                environment.get("CLAUDE_CODE_TMPDIR")
                if isinstance(environment, Mapping)
                else None
            )
            if temporary := _directory(configured_tmp):
                excluded.append(Path(temporary))
    except ProjectError as exc:
        warnings.append(str(exc))
    config = Path(os.environ.get("SESSION_KIT_CLAUDE_CONFIG", home / ".claude.json"))
    try:
        payload = _read_owner_file(
            config,
            label="Claude Code project configuration",
            max_bytes=MAX_PROVIDER_CONFIG_BYTES,
        )
        if payload is not None:
            projects = _json_object(payload, "Claude Code project configuration").get(
                "projects"
            )
            if isinstance(projects, Mapping):
                paths.update(
                    path
                    for raw in projects
                    if (path := _outside_roots(_directory(raw), excluded))
                )
    except ProjectError as exc:
        warnings.append(str(exc))

    history = Path(
        os.environ.get("SESSION_KIT_CLAUDE_HISTORY", home / ".claude" / "history.jsonl")
    )
    try:
        payload = _read_owner_file(
            history, label="Claude Code history", max_bytes=MAX_CLAUDE_HISTORY_BYTES
        )
        if payload is not None:
            for line in payload.splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, Mapping) and (
                    path := _outside_roots(_directory(row.get("project")), excluded)
                ):
                    paths.add(path)
    except ProjectError as exc:
        warnings.append(str(exc))
    return paths


def _toml_projects(payload: bytes) -> set[object]:
    text = payload.decode("utf-8", "strict")
    if tomllib is not None:
        parsed = tomllib.loads(text)
        projects = parsed.get("projects")
        return set(projects) if isinstance(projects, Mapping) else set()
    found: set[object] = set()
    for line in text.splitlines():
        match = CODEX_PROJECT_HEADER_RE.fullmatch(line)
        if not match:
            continue
        encoded = match.group(1)
        if encoded.startswith('"'):
            try:
                found.add(json.loads(encoded))
            except ValueError:
                continue
        else:
            found.add(encoded[1:-1])
    return found


def _codex_databases(codex_home: Path) -> list[Path]:
    candidates: list[tuple[int, str, Path]] = []
    try:
        entries = list(codex_home.iterdir())
    except OSError:
        return []
    for path in entries:
        match = re.fullmatch(r"state_(\d+)\.sqlite", path.name)
        if match and path.is_file() and not path.is_symlink():
            candidates.append((int(match.group(1)), path.name, path))
    return [row[2] for row in sorted(candidates)]


def _codex_db_paths(database: Path, warnings: list[str]) -> set[str]:
    try:
        info = database.stat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise ProjectError(f"unsafe Codex state database: {database}")
        uri = f"file:{quote(os.fspath(database.resolve()))}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            connection.execute("PRAGMA query_only=ON")
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(threads)")
            }
            if "cwd" not in columns:
                raise ProjectError(
                    "Codex threads table has no project directory column"
                )
            return {
                path
                for (raw,) in connection.execute(
                    "SELECT DISTINCT cwd FROM threads WHERE cwd IS NOT NULL AND cwd != ''"
                )
                if (path := _directory(raw))
            }
        finally:
            connection.close()
    except (OSError, sqlite3.Error, ProjectError) as exc:
        warnings.append(str(exc))
        return set()


def _codex_paths(home: Path, warnings: list[str]) -> set[str]:
    codex_home = Path(os.environ.get("SESSION_KIT_CODEX_HOME", home / ".codex"))
    paths: set[str] = set()
    config = codex_home / "config.toml"
    try:
        payload = _read_owner_file(
            config, label="Codex configuration", max_bytes=MAX_PROVIDER_CONFIG_BYTES
        )
        if payload is not None:
            paths.update(
                path for raw in _toml_projects(payload) if (path := _directory(raw))
            )
    except (ProjectError, UnicodeDecodeError, ValueError) as exc:
        warnings.append(f"Codex configuration could not be read: {exc}")
    databases = _codex_databases(codex_home)
    if databases:
        paths.update(_codex_db_paths(databases[-1], warnings))
    return paths


def discover_projects(home: Path | None = None) -> dict[str, Any]:
    root = (home or Path(os.environ.get("HOME", Path.home()))).resolve()
    warnings: list[str] = []
    records = [
        {"provider": provider, "cwd": cwd}
        for provider, paths in (
            ("claude", _claude_paths(root, warnings)),
            ("codex", _codex_paths(root, warnings)),
        )
        for cwd in paths
    ]
    records.sort(key=lambda row: (row["cwd"].casefold(), row["provider"]))
    if len(records) > MAX_PROJECTS:
        warnings.append(f"project discovery was limited to {MAX_PROJECTS} records")
        records = records[:MAX_PROJECTS]
    return {"projects": records, "warnings": sorted(set(warnings))}


def _parse_projects(payload: bytes) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in payload.decode("utf-8", "strict").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 3:
            continue
        alias, provider, cwd = fields[:3]
        if ALIAS_RE.fullmatch(alias) and provider in (*PROVIDERS, "shell"):
            rows.append({"alias": alias, "provider": provider, "cwd": cwd})
    return rows


def _slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return value[:48].strip("-") or "project"


def _path_aliases(paths: Iterable[str]) -> dict[str, str]:
    unique = sorted(set(paths))
    components = {
        path: [_slug(part) for part in Path(path).parts if part not in ("/", "")]
        for path in unique
    }
    result: dict[str, str] = {}
    for path in unique:
        parts = components[path]
        first_width = (
            2 if len(parts) > 1 and parts[-1] in GENERIC_DIRECTORY_NAMES else 1
        )
        for width in range(first_width, min(len(parts), 4) + 1):
            candidate = "-".join(parts[-width:])
            if (
                sum(
                    "-".join(other[-width:]) == candidate
                    for other in components.values()
                    if len(other) >= width
                )
                == 1
            ):
                result[path] = candidate
                break
        else:
            result[path] = "-".join(parts[-4:])
    return result


def _new_rows(
    discovered: Sequence[Mapping[str, str]], existing: Sequence[Mapping[str, str]]
) -> list[dict[str, str]]:
    existing_pairs = {(row["provider"], row["cwd"]) for row in existing}
    reserved = {row["alias"] for row in existing}
    pending = [
        {"provider": row["provider"], "cwd": row["cwd"]}
        for row in discovered
        if (row["provider"], row["cwd"]) not in existing_pairs
    ]
    bases = _path_aliases(row["cwd"] for row in pending)
    providers_by_path: dict[str, set[str]] = {}
    for row in pending:
        providers_by_path.setdefault(row["cwd"], set()).add(row["provider"])
    added: list[dict[str, str]] = []
    for row in pending:
        base = bases[row["cwd"]]
        if len(providers_by_path[row["cwd"]]) > 1:
            base = f"{base}-{row['provider']}"
        alias = base
        if alias in reserved:
            alias = f"{base}-{row['provider']}"
        counter = 2
        while alias in reserved:
            alias = f"{base}-{counter}"
            counter += 1
        reserved.add(alias)
        added.append({"alias": alias, **row})
    return added


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _backup(payload: bytes, state_dir: Path) -> str:
    backup_dir = state_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    for serial in range(1000):
        path = backup_dir / f"projects-{stamp}-{os.getpid()}-{serial}.tsv"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return os.fspath(path)
    raise ProjectError("cannot allocate a unique projects backup")


def _mutate_projects(
    projects_file: Path,
    state_dir: Path,
    mutate: Any,
) -> dict[str, Any]:
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = state_dir / "projects.lock"
    lock_descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        original = _read_owner_file(
            projects_file,
            label="Session Kit projects file",
            max_bytes=MAX_PROJECTS_FILE_BYTES,
        )
        if original is None:
            original = b"# alias<TAB>provider<TAB>absolute cwd\n"
        try:
            existing = _parse_projects(original)
        except (UnicodeDecodeError, ValueError) as exc:
            raise ProjectError("Session Kit projects file is not valid UTF-8") from exc
        added = mutate(existing)
        if not added:
            return {"added": [], "backup": None}
        payload = original
        if payload and not payload.endswith(b"\n"):
            payload += b"\n"
        payload += "".join(
            f"{row['alias']}\t{row['provider']}\t{row['cwd']}\n" for row in added
        ).encode("utf-8")
        backup = _backup(original, state_dir) if projects_file.exists() else None
        _atomic_write(projects_file, payload)
        return {"added": added, "backup": backup}
    finally:
        os.close(lock_descriptor)


def import_projects(projects_file: Path, state_dir: Path) -> dict[str, Any]:
    discovery = discover_projects()

    def merge(existing: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
        return _new_rows(discovery["projects"], existing)

    result = _mutate_projects(projects_file, state_dir, merge)
    return {**result, **discovery}


def add_project(
    projects_file: Path,
    state_dir: Path,
    alias: str,
    provider: str,
    cwd: str,
) -> dict[str, Any]:
    if not ALIAS_RE.fullmatch(alias):
        raise ProjectError("alias must use lowercase letters, numbers, _ or -")
    if provider not in (*PROVIDERS, "shell"):
        raise ProjectError("provider must be claude, codex, or shell")
    directory = _directory(cwd)
    if directory is None:
        raise ProjectError("project directory must be an existing absolute directory")

    def add(existing: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
        for row in existing:
            if row["alias"] == alias:
                if row["provider"] == provider and row["cwd"] == directory:
                    return []
                raise ProjectError(f"project alias already exists: {alias}")
        return [{"alias": alias, "provider": provider, "cwd": directory}]

    return _mutate_projects(projects_file, state_dir, add)


def _human_discovery(value: Mapping[str, Any]) -> None:
    projects = value.get("projects", [])
    if not projects:
        print("No existing Claude Code or Codex project folders were found.")
    else:
        print(f"Found {len(projects)} existing provider project folder(s):")
        for row in projects:
            print(f"  {row['provider'].capitalize():6}  {row['cwd']}")
    for warning in value.get("warnings", []):
        print(f"Warning: {warning}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--projects-file", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--json", action="store_true")
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("discover")
    subparsers.add_parser("import")
    add = subparsers.add_parser("add")
    add.add_argument("alias")
    add.add_argument("provider")
    add.add_argument("cwd")
    subparsers.add_parser("list")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    home = Path(os.environ.get("HOME", Path.home()))
    projects_file = args.projects_file or Path(
        os.environ.get(
            "SESSION_KIT_PROJECTS_FILE",
            Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
            / "session-kit"
            / "projects.tsv",
        )
    )
    state_dir = args.state_dir or Path(
        os.environ.get(
            "SESSION_KIT_STATE_DIR",
            Path(os.environ.get("XDG_STATE_HOME", home / ".local" / "state"))
            / "session-kit",
        )
    )
    try:
        if args.action == "discover":
            value = discover_projects()
        elif args.action == "import":
            value = import_projects(projects_file, state_dir)
        elif args.action == "add":
            value = add_project(
                projects_file, state_dir, args.alias, args.provider, args.cwd
            )
        else:
            payload = _read_owner_file(
                projects_file,
                label="Session Kit projects file",
                max_bytes=MAX_PROJECTS_FILE_BYTES,
            )
            value = {"projects": _parse_projects(payload or b"")}
        if args.json:
            json.dump(value, fp=sys.stdout, indent=2, sort_keys=True)
            print()
        elif args.action == "discover":
            _human_discovery(value)
        elif args.action == "import":
            _human_discovery(value)
            print(f"Imported {len(value['added'])} new project shortcut(s).")
            if value.get("backup"):
                print(f"Previous projects file backed up to {value['backup']}")
        elif args.action == "add":
            if value["added"]:
                row = value["added"][0]
                print(f"Added {row['alias']}: {row['provider']} in {row['cwd']}")
            else:
                print("That project shortcut is already configured.")
        else:
            for row in value["projects"]:
                print(f"{row['alias']}\t{row['provider']}\t{row['cwd']}")
        return 0
    except (OSError, ProjectError, sqlite3.Error, ValueError) as exc:
        print(f"session-kit projects: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
