"""One project identity for the two senses the kit grew separately.

Session Kit had two unrelated things called a project:

* a shortcut row in ``projects.tsv`` — an alias, one default provider, and one
  absolute directory, discovered from what the providers had already run;
* a supervisor intake — a unit of delegated work whose only sense of place is
  the ``source_cwd`` it arrived from, with no name and no link to any row.

Session rows carried a third, derived notion: a title hint computed from the
last interesting path component, which never consulted either of the above.

This module makes those one thing. A project is a **canonical absolute root
directory**. Every other record — a shortcut alias, a manifest, an intake, a
live session — belongs to the project whose root is the deepest ancestor (or
equal) of its directory. That single membership rule is what lets the picker
group sessions, the supervisor place an intake, and ``sp new`` resume a
context without any of them inventing their own idea of a project.

Trust
-----
A ``session-kit.toml`` is repository content. Reading one is always safe;
letting one decide what launches is not, because cloning a repository would
otherwise be enough to choose a model, an account, and a startup command on
this host. So a manifest's launch fields apply only when the project root is
**host-trusted**, and the trust record is the existing deliberate act of
adding the project: a non-ignore ``projects.tsv`` row for that exact root, or
for the main repository of a linked git worktree. An untrusted manifest is
still parsed, reported, and shown — it just never changes a launch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from . import manifest as manifest_module
from .manifest import MANIFEST_NAME, ManifestError

# A repository sits a few levels below a home directory, not a few hundred.
# The bound also ends the walk on a cyclic mount rather than spinning.
MAX_WALK_DEPTH = 64
IGNORE_KIND = "ignore"
# A live row's kind is the project's default provider, and "any" is the row
# that has none: one directory is one project, and which provider opens it is
# answered when a session starts.
ANY_KIND = "any"
GITDIR_RE = re.compile(r"\A\s*gitdir:\s*(?P<path>\S.*?)\s*\Z")
MAX_GIT_POINTER_BYTES = 4096

SOURCE_MANIFEST = "manifest"
SOURCE_SHORTCUT = "shortcut"


@dataclass(frozen=True)
class Project:
    """A resolved project identity.

    ``root`` is the canonical directory that defines the project. ``alias``
    is the shortcut name when the host has one, ``name`` the display name
    (manifest name, alias, or the directory's own name, in that order).
    ``group_root`` is the main repository when ``root`` is a linked git
    worktree, so worktrees group under the project they were cut from.
    """

    root: str
    name: str
    source: str
    alias: str | None = None
    group_root: str | None = None
    description: str | None = None
    trusted: bool = False
    manifest: Mapping[str, Any] | None = None
    manifest_path: str | None = None
    manifest_error: str | None = None
    shortcut_provider: str | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_worktree(self) -> bool:
        return self.group_root is not None and self.group_root != self.root

    @property
    def group(self) -> str:
        return self.group_root or self.root

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "name": self.name,
            "source": self.source,
            "alias": self.alias,
            "group_root": self.group,
            "is_worktree": self.is_worktree,
            "description": self.description,
            "trusted": self.trusted,
            "manifest": dict(self.manifest) if self.manifest is not None else None,
            "manifest_path": self.manifest_path,
            "manifest_error": self.manifest_error,
            "shortcut_provider": self.shortcut_provider,
            "warnings": list(self.warnings),
        }


# ---- paths --------------------------------------------------------------


def canonical(path: str | os.PathLike[str]) -> str | None:
    """Resolve a directory the way every record here stores it, or ``None``.

    Symlinks are resolved so that two records naming the same directory by
    different routes are one project rather than two.
    """
    try:
        resolved = Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None
    text = os.fspath(resolved)
    if not text.startswith("/"):
        return None
    return text


def _within(child: str, ancestor: str) -> bool:
    """Is ``child`` the directory ``ancestor`` or inside it?

    Compared on path components, so ``/srv/song`` is not inside ``/srv/so``.
    """
    if child == ancestor:
        return True
    prefix = ancestor if ancestor.endswith("/") else ancestor + "/"
    return child.startswith(prefix)


def depth(path: str) -> int:
    return len([part for part in path.split("/") if part])


# ---- git worktrees ------------------------------------------------------


def main_repository(root: str) -> str | None:
    """The main repository of a linked git worktree, or ``None``.

    A linked worktree has a ``.git`` *file* holding ``gitdir: <main>/.git/
    worktrees/<name>``. Reading that pointer is enough; running ``git`` would
    execute a binary chosen by ``PATH`` for what is a one-line file read.
    """
    pointer = Path(root) / ".git"
    try:
        if pointer.is_symlink() or not pointer.is_file():
            return None
        if pointer.stat().st_size > MAX_GIT_POINTER_BYTES:
            return None
        text = pointer.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    match = GITDIR_RE.match(text.strip())
    if not match:
        return None
    target = match.group("path")
    if not target.startswith("/"):
        target = os.path.normpath(os.path.join(root, target))
    marker = "/.git/worktrees/"
    index = target.find(marker)
    if index <= 0:
        return None
    return canonical(target[:index])


# ---- the shortcut table -------------------------------------------------


def _shortcut_rows(
    projects_file: Path | None,
) -> tuple[list[dict[str, str]], list[str]]:
    """Read ``projects.tsv`` into canonical rows, tolerating a missing file.

    Parsing mirrors ``sessionkit_inventory.projects``: three tab fields, a
    known kind, an absolute directory. A row this cannot read is skipped with
    a warning rather than failing the whole resolution — a project list is a
    convenience, and one bad hand-edited line must not blind the picker to
    every other project.
    """
    warnings: list[str] = []
    if projects_file is None:
        return [], warnings
    try:
        if projects_file.is_symlink() or not projects_file.is_file():
            return [], warnings
        payload = projects_file.read_bytes()
    except OSError as error:
        return [], [f"the project list is unreadable: {error.strerror}"]
    try:
        text = payload.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return [], ["the project list is not valid UTF-8"]
    rows: list[dict[str, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 3:
            warnings.append(f"project list line {number} is not three tab fields")
            continue
        alias, kind, raw_cwd = fields[0], fields[1], fields[2]
        if kind not in ("claude", "codex", "shell", ANY_KIND, IGNORE_KIND):
            warnings.append(f"project list line {number} has an unknown kind")
            continue
        if not raw_cwd.startswith("/"):
            warnings.append(f"project list line {number} is not an absolute directory")
            continue
        resolved = canonical(raw_cwd)
        if resolved is None:
            warnings.append(f"project list line {number} has an unusable directory")
            continue
        rows.append({"alias": alias, "kind": kind, "cwd": resolved})
    return rows, warnings


# ---- resolution ---------------------------------------------------------


class Resolver:
    """Resolve directories and aliases to project identities.

    One instance reads the shortcut table once, so resolving every row of a
    session list costs one file read rather than one per row.
    """

    def __init__(
        self,
        projects_file: Path | str | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        environ = os.environ if environ is None else environ
        if projects_file is None:
            projects_file = default_projects_file(environ)
        self.projects_file = Path(projects_file) if projects_file is not None else None
        self._rows, self._warnings = _shortcut_rows(self.projects_file)
        self._manifests: dict[
            str, tuple[Mapping[str, Any] | None, str | None, str | None]
        ] = {}

    # ---- shortcut table --------------------------------------------------

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(self._warnings)

    def shortcuts(self) -> list[dict[str, str]]:
        return [dict(row) for row in self._rows if row["kind"] != IGNORE_KIND]

    def _shortcut_for_root(self, root: str) -> dict[str, str] | None:
        for row in self._rows:
            if row["kind"] != IGNORE_KIND and row["cwd"] == root:
                return row
        return None

    def _deepest_shortcut(self, directory: str) -> dict[str, str] | None:
        best: dict[str, str] | None = None
        for row in self._rows:
            if row["kind"] == IGNORE_KIND or not _within(directory, row["cwd"]):
                continue
            if best is None or depth(row["cwd"]) > depth(best["cwd"]):
                best = row
        return best

    def is_ignored(self, directory: str) -> bool:
        return any(
            row["kind"] == IGNORE_KIND and row["cwd"] == directory for row in self._rows
        )

    # ---- manifests -------------------------------------------------------

    def _manifest_at(
        self, directory: str
    ) -> tuple[Mapping[str, Any] | None, str | None, str | None]:
        """``(values, path, error)`` for a manifest in ``directory``."""
        cached = self._manifests.get(directory)
        if cached is not None:
            return cached
        path = Path(directory) / MANIFEST_NAME
        result: tuple[Mapping[str, Any] | None, str | None, str | None]
        try:
            present = path.is_file() or path.is_symlink()
        except OSError:
            present = False
        if not present:
            result = (None, None, None)
        else:
            try:
                result = (manifest_module.read(path), os.fspath(path), None)
            except ManifestError as error:
                # A malformed manifest is reported, never guessed around: the
                # project still resolves so the directory does not vanish from
                # the picker, but nothing it asked for is applied.
                result = (None, os.fspath(path), str(error))
        self._manifests[directory] = result
        return result

    def _manifest_root(
        self, directory: str
    ) -> tuple[str, Mapping[str, Any] | None, str | None, str | None] | None:
        """Walk up from ``directory`` to the nearest manifest."""
        current = directory
        for _ in range(MAX_WALK_DEPTH):
            values, path, error = self._manifest_at(current)
            if path is not None:
                root = current
                if values is not None and values.get("root") not in (None, "."):
                    candidate = canonical(os.path.join(current, str(values["root"])))
                    if candidate is not None and _within(candidate, current):
                        root = candidate
                return root, values, path, error
            parent = os.path.dirname(current)
            if not parent or parent == current:
                return None
            current = parent
        return None

    # ---- trust -----------------------------------------------------------

    def trusts(self, root: str, group_root: str | None = None) -> bool:
        """Has the host deliberately added this project?

        A linked worktree inherits the trust of the repository it was cut
        from: ``sp new --worktree`` creates directories the host never listed,
        and refusing them would make the manifest useless exactly where
        delegated work runs.
        """
        if self._shortcut_for_root(root) is not None:
            return True
        return (
            group_root is not None and self._shortcut_for_root(group_root) is not None
        )

    # ---- the public entry points ----------------------------------------

    def resolve(self, path: str | os.PathLike[str]) -> Project | None:
        """The project a directory belongs to, or ``None`` if it has none."""
        directory = canonical(path)
        if directory is None:
            return None
        found = self._manifest_root(directory)
        if found is not None:
            root, values, manifest_path, error = found
            return self._project(
                root,
                source=SOURCE_MANIFEST,
                values=values,
                manifest_path=manifest_path,
                error=error,
            )
        row = self._deepest_shortcut(directory)
        if row is None:
            return None
        return self._project(row["cwd"], source=SOURCE_SHORTCUT)

    def resolve_alias(self, alias: str) -> Project | None:
        """The project a shortcut alias names, manifest included.

        The row that matched is carried through. Looking the directory up again
        would answer with the *first* row for it, so on a file that still lists
        one directory twice — the shape this release folds — asking for the
        second alias returned the first alias's provider and started the wrong
        one.
        """
        for row in self._rows:
            if row["alias"] != alias or row["kind"] == IGNORE_KIND:
                continue
            values, manifest_path, error = self._manifest_at(row["cwd"])
            source = SOURCE_MANIFEST if manifest_path is not None else SOURCE_SHORTCUT
            return self._project(
                row["cwd"],
                source=source,
                values=values,
                manifest_path=manifest_path,
                error=error,
                row=row,
            )
        return None

    def projects(self) -> list[Project]:
        """Every project the host knows: one per non-ignore shortcut row."""
        seen: set[str] = set()
        found: list[Project] = []
        for row in self._rows:
            if row["kind"] == IGNORE_KIND or row["cwd"] in seen:
                continue
            seen.add(row["cwd"])
            values, manifest_path, error = self._manifest_at(row["cwd"])
            source = SOURCE_MANIFEST if manifest_path is not None else SOURCE_SHORTCUT
            found.append(
                self._project(
                    row["cwd"],
                    source=source,
                    values=values,
                    manifest_path=manifest_path,
                    error=error,
                )
            )
        found.sort(key=lambda project: (project.name, project.root))
        return found

    def assign(self, directories: Iterable[str]) -> dict[str, Project | None]:
        """Resolve many directories at once, for grouping a session list."""
        result: dict[str, Project | None] = {}
        for directory in directories:
            if directory in result:
                continue
            result[directory] = self.resolve(directory)
        return result

    # ---- construction ----------------------------------------------------

    def _live_rows_for_root(self, root: str) -> list[dict[str, str]]:
        return [
            item
            for item in self._rows
            if item["kind"] != IGNORE_KIND and item["cwd"] == root
        ]

    def _project(
        self,
        root: str,
        *,
        source: str,
        values: Mapping[str, Any] | None = None,
        manifest_path: str | None = None,
        error: str | None = None,
        row: Mapping[str, str] | None = None,
    ) -> Project:
        siblings = self._live_rows_for_root(root)
        row = dict(row) if row is not None else self._shortcut_for_root(root)
        group_root = main_repository(root)
        warnings: list[str] = []
        manifest_values = values
        if manifest_values is not None and manifest_path is None:
            manifest_values = None
        name = None
        description = None
        if manifest_values is not None:
            name = manifest_values.get("name")
            description = manifest_values.get("description")
        if not name and row is not None:
            name = row["alias"]
        if not name:
            name = Path(root).name or root
        trusted = self.trusts(root, group_root)
        if manifest_path is not None and error is None and not trusted:
            warnings.append(
                "this project has a session-kit.toml but is not on the host's "
                "project list, so its launch settings are shown, not applied; "
                "add it with `session-kit projects add` to apply them"
            )
        if error is not None:
            warnings.append(error)
        shortcut_provider = row["kind"] if row is not None else None
        if len({item["kind"] for item in siblings}) > 1:
            # The directory is still listed more than once and the rows
            # disagree about the provider. There is no default to be had from
            # that, and picking either row's answer is how a directory worked
            # on with both providers started the wrong one.
            shortcut_provider = ANY_KIND
            warnings.append(
                f"{root} is listed more than once with different providers, so "
                "it has no default; name one when you start a session, or run "
                "`session-kit projects normalize`"
            )
        return Project(
            root=root,
            name=str(name),
            source=source,
            alias=row["alias"] if row is not None else None,
            group_root=group_root,
            description=str(description) if description else None,
            trusted=trusted,
            manifest=manifest_values,
            manifest_path=manifest_path,
            manifest_error=error,
            shortcut_provider=shortcut_provider,
            warnings=tuple(warnings),
        )


def default_projects_file(environ: Mapping[str, str] | None = None) -> Path:
    """The same path ``bin/session_kit_common`` resolves, and by the same rules."""
    environ = os.environ if environ is None else environ
    override = environ.get("SESSION_KIT_PROJECTS_FILE")
    if override:
        return Path(override)
    config_home = environ.get("XDG_CONFIG_HOME") or os.path.join(
        environ.get("HOME", str(Path.home())), ".config"
    )
    return Path(config_home) / "session-kit" / "projects.tsv"


def group_projects(
    projects: Sequence[Project],
) -> list[dict[str, Any]]:
    """Collapse worktrees under the project they were cut from.

    The picker groups sessions by project; a project cut into four delegated
    worktrees should read as one project with four working copies, not five
    unrelated rows.
    """
    groups: dict[str, dict[str, Any]] = {}
    for project in projects:
        entry = groups.setdefault(
            project.group,
            {"group_root": project.group, "name": project.name, "members": []},
        )
        if project.root == project.group:
            entry["name"] = project.name
        entry["members"].append(project.to_dict())
    for entry in groups.values():
        entry["members"].sort(key=lambda member: str(member["root"]))
    return sorted(
        groups.values(),
        key=lambda entry: (str(entry["name"]), str(entry["group_root"])),
    )
