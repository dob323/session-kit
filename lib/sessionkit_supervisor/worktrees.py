"""Git worktree isolation for delegated workers.

A delegated worker already has a branch: the reviewed preflight names one per
workstream, and the intake entry records it before anything launches. What the
worker never had was a directory of its own, so two workers on one project
edited the same files at the same time and the branch existed only as a label.
This module materializes that already-recorded branch as a real worktree,
records it, and prunes it again once the work is merged.

Three rules the code enforces rather than documents:

* Nothing outside the kit's own worktree root is ever removed. Teardown reads
  a registry record, checks the recorded path is inside that root, and refuses
  anything else — a hand-made worktree elsewhere is the operator's, not ours.
* Dirty is refused. Uncommitted or untracked files in a worktree are somebody's
  unsaved work; teardown reports them and stops instead of taking the decision.
* Unmerged is refused. `--merged-into` names the ref the branch has to be an
  ancestor of before its directory can go. Commits are never destroyed either
  way: teardown removes the worktree, never the branch.

The worktrees live under the kit's state directory rather than beside the
repository on purpose. A sibling directory inside the project would show up as
untracked in `git status` and in the installer's own clean-tree gate.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any

from .private_store import (
    PrivateStoreError,
    private_directory,
    private_names,
    read_private_json,
    write_private_json,
)

SCHEMA_VERSION = 1
# Same branch grammar the intake preflight validates worker branches with, so a
# branch that reaches a worker plan can always be materialized.
BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}")
MAX_RECORD_BYTES = 8192
MAX_RECORDS = 512
GIT_TIMEOUT_SECONDS = 60

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class WorktreeError(ValueError):
    """A worktree request is invalid, unsafe, or refused by git."""


def now_unix_ms(clock: Callable[[], float] = time.time) -> int:
    return int(clock() * 1000)


# ---- paths --------------------------------------------------------------


def valid_branch(value: object) -> str:
    if not isinstance(value, str) or not BRANCH_RE.fullmatch(value.strip()):
        raise WorktreeError(
            "a worktree branch must be 1-128 characters of A-Z a-z 0-9 . _ / - "
            "and start with a letter or digit"
        )
    branch = value.strip()
    if branch.endswith("/") or ".." in branch or "//" in branch:
        raise WorktreeError(f"branch {branch} is not a usable git ref name")
    return branch


def slug(value: str, *, limit: int = 48) -> str:
    """One filesystem-safe component that still reads like its source."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.") or "worktree"
    return cleaned[:limit]


def worktree_root(
    state_dir: Path | str, environ: Mapping[str, str] | None = None
) -> Path:
    """Where materialized worktrees live; overridable for tests and operators."""
    override = (environ or os.environ).get("SESSION_KIT_WORKTREE_ROOT", "").strip()
    if override:
        root = Path(override)
        if not root.is_absolute():
            raise WorktreeError("SESSION_KIT_WORKTREE_ROOT must be an absolute path")
        return root
    return Path(state_dir) / "worktrees"


def _trees_dir(root: Path) -> Path:
    return private_directory(root / "trees", label="worktree root")


def _registry_dir(root: Path) -> Path:
    return private_directory(root / "registry", label="worktree registry")


def token_for(repo: Path, branch: str) -> str:
    digest = hashlib.sha256(f"{repo}\0{branch}".encode("utf-8")).hexdigest()
    return digest[:32]


# ---- git ----------------------------------------------------------------


def _git(
    runner: Runner, cwd: Path, *arguments: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    completed = runner(
        ["git", "-C", os.fspath(cwd), *arguments],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
    )
    if check and completed.returncode != 0:
        detail = " ".join(
            (completed.stderr or completed.stdout or "").split()
        )[:300]
        raise WorktreeError(
            f"git {' '.join(arguments)} failed ({completed.returncode}): {detail}"
        )
    return completed


def repository_root(
    path: Path | str, *, runner: Runner = subprocess.run
) -> Path | None:
    """The main working tree the directory belongs to, or None when it is not one.

    None is a real answer here — the launcher asks this question exactly so it
    can say in the receipt that a project has no repository to isolate in,
    instead of failing a launch or pretending isolation happened.

    A directory inside a linked worktree resolves to the *main* working tree,
    not to itself. Worktrees are siblings, never nested: asking from inside one
    and getting itself back would key a second registry record for the same
    repository and leave `git worktree add` building a worktree of a worktree.
    """
    directory = Path(path)
    if not directory.is_dir():
        return None
    completed = _git(
        runner, directory, "rev-parse", "--path-format=absolute",
        "--show-toplevel", "--git-dir", "--git-common-dir",
        check=False,
    )
    if completed.returncode != 0:
        return None
    lines = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
    if len(lines) != 3:
        return None
    top, git_dir, common_dir = (Path(value) for value in lines)
    if git_dir != common_dir and common_dir.name == ".git":
        return common_dir.parent
    return top


def _branch_exists(runner: Runner, repo: Path, branch: str) -> bool:
    completed = _git(
        runner, repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
        check=False,
    )
    return completed.returncode == 0


def _checked_out_at(runner: Runner, repo: Path, branch: str) -> str | None:
    """The path where the branch is already checked out, if any."""
    completed = _git(runner, repo, "worktree", "list", "--porcelain")
    path: str | None = None
    for line in (completed.stdout or "").splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree ") :].strip()
        elif line.startswith("branch ") and path is not None:
            reference = line[len("branch ") :].strip()
            if reference == f"refs/heads/{branch}":
                return path
    return None


# ---- registry -----------------------------------------------------------


def _record_path(root: Path, token: str) -> Path:
    return _registry_dir(root) / f"{token}.json"


def _validated_record(document: Mapping[str, Any], path: Path) -> dict[str, Any]:
    required = ("token", "repo", "branch", "path", "created_unix_ms")
    missing = [key for key in required if not document.get(key)]
    if missing:
        raise WorktreeError(f"worktree record is missing {', '.join(missing)}: {path}")
    record = dict(document)
    for key in ("repo", "path"):
        value = record[key]
        if not isinstance(value, str) or not value.startswith("/"):
            raise WorktreeError(f"worktree record {key} must be absolute: {path}")
    record["branch"] = valid_branch(record["branch"])
    return record


def read_record(root: Path, token: str) -> dict[str, Any] | None:
    path = _record_path(root, token)
    document = read_private_json(
        path, limit=MAX_RECORD_BYTES, label="worktree record"
    )
    if document is None:
        return None
    return _validated_record(document, path)


def records(state_dir: Path | str, environ: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Every recorded worktree, newest first."""
    root = worktree_root(state_dir, environ)
    registry = root / "registry"
    if not registry.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for token in private_names(registry, ".json", limit=MAX_RECORDS):
        try:
            record = read_record(root, token)
        except (WorktreeError, PrivateStoreError):
            # A damaged record must not hide the healthy ones from a listing;
            # `teardown` still refuses to act on anything it cannot validate.
            continue
        if record is not None:
            found.append(record)
    found.sort(key=lambda item: int(item.get("created_unix_ms") or 0), reverse=True)
    return found


def lookup(
    state_dir: Path | str,
    *,
    path: Path | str | None = None,
    repo: Path | str | None = None,
    branch: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """One recorded worktree by its directory, or by repository and branch."""
    root = worktree_root(state_dir, environ)
    if repo is not None and branch is not None:
        return read_record(root, token_for(Path(repo), valid_branch(branch)))
    if path is None:
        raise WorktreeError("a worktree lookup needs a path, or a repo and branch")
    wanted = os.fspath(Path(path))
    for record in records(state_dir, environ):
        if record["path"] == wanted:
            return record
    return None


def labels(
    state_dir: Path | str, environ: Mapping[str, str] | None = None
) -> dict[str, dict[str, str]]:
    """Worktree directory -> what a row can say about it.

    ``path`` is in the value as well as the key so a consumer holding one
    annotation can name the working copy itself, not only the repository it
    belongs to — the project view places a session by `repo` and then has a
    real directory to show for it.
    """
    return {
        record["path"]: {
            "branch": record["branch"],
            "repo": record["repo"],
            "path": record["path"],
        }
        for record in records(state_dir, environ)
    }


# ---- verbs --------------------------------------------------------------


def preflight(
    *,
    repo: Path | str,
    branch: str,
    state_dir: Path | str,
    environ: Mapping[str, str] | None = None,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Whether this branch could be materialized, without creating anything.

    The delegate path asks this before it reserves or dispatches anything. Once
    a worker row is marked dispatching, a refusal from inside the launcher
    reads as an uncertain dispatch and costs somebody a manual reconcile — so
    the answer has to be available while refusing is still free.
    """
    wanted = valid_branch(branch)
    top = repository_root(repo, runner=runner)
    if top is None:
        return {
            "repo": os.fspath(Path(repo)),
            "branch": wanted,
            "path": "",
            "exists": False,
            "blocked_by": "",
            "reason": "not a git repository",
        }
    root = worktree_root(state_dir, environ)
    destination = root / "trees" / slug(top.name) / slug(wanted)
    occupied = _checked_out_at(runner, top, wanted)
    blocked = (
        occupied if occupied is not None and Path(occupied) != destination else ""
    )
    return {
        "repo": os.fspath(top),
        "branch": wanted,
        "path": os.fspath(destination),
        "exists": destination.is_dir(),
        "blocked_by": blocked,
        "reason": (
            f"branch {wanted} is already checked out at {blocked}" if blocked else ""
        ),
    }


def materialize(
    *,
    repo: Path | str,
    branch: str,
    state_dir: Path | str,
    start_ref: str = "HEAD",
    environ: Mapping[str, str] | None = None,
    runner: Runner = subprocess.run,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Materialize the already-recorded branch as its own worktree.

    Idempotent by (repository, branch): a second call for a worktree that is
    still on disk returns the same record with ``created`` false, so a retried
    delegate never forks a worker's directory.
    """
    wanted_branch = valid_branch(branch)
    top = repository_root(repo, runner=runner)
    if top is None:
        raise WorktreeError(f"{repo} is not inside a git repository")
    root = worktree_root(state_dir, environ)
    private_directory(root, label="worktree root")
    token = token_for(top, wanted_branch)
    existing = read_record(root, token)
    if existing is not None:
        current = Path(existing["path"])
        if current.is_dir() and not current.is_symlink():
            return {**existing, "created": False}
        # The directory is gone from under a live record: let git forget it,
        # then fall through and build it again at the recorded path.
        _git(runner, top, "worktree", "prune", check=False)
    trees = _trees_dir(root)
    destination = trees / slug(top.name) / slug(wanted_branch)
    occupied = _checked_out_at(runner, top, wanted_branch)
    if occupied is not None and Path(occupied) != destination:
        raise WorktreeError(
            f"branch {wanted_branch} is already checked out at {occupied}"
        )
    if occupied is None:
        private_directory(destination.parent, label="worktree root")
        if destination.exists() or destination.is_symlink():
            raise WorktreeError(f"worktree destination already exists: {destination}")
        arguments = ["worktree", "add"]
        if _branch_exists(runner, top, wanted_branch):
            arguments += [os.fspath(destination), wanted_branch]
        else:
            arguments += ["-b", wanted_branch, os.fspath(destination), start_ref]
        _git(runner, top, *arguments)
    record = {
        "schema_version": SCHEMA_VERSION,
        "token": token,
        "repo": os.fspath(top),
        "branch": wanted_branch,
        "path": os.fspath(destination),
        "created_unix_ms": now_unix_ms(clock),
        "shpool_id": "",
        "launch_key": "",
    }
    write_private_json(_record_path(root, token), record)
    return {**record, "created": True}


def bind(
    *,
    state_dir: Path | str,
    path: Path | str,
    shpool_id: str = "",
    launch_key: str = "",
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Attach the session that owns a worktree to its record."""
    record = lookup(state_dir, path=path, environ=environ)
    if record is None:
        raise WorktreeError(f"no recorded worktree at {path}")
    identifier = shpool_id.strip()
    key = launch_key.strip()
    for value, label in ((identifier, "shpool id"), (key, "launch key")):
        if value and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
            raise WorktreeError(f"worktree {label} is not a supported identifier")
    if identifier:
        record["shpool_id"] = identifier
    if key:
        record["launch_key"] = key
    root = worktree_root(state_dir, environ)
    write_private_json(_record_path(root, str(record["token"])), record)
    return record


def status(
    *, path: Path | str, runner: Runner = subprocess.run
) -> list[str]:
    """Uncommitted and untracked entries in one worktree, bounded for display."""
    completed = _git(runner, Path(path), "status", "--porcelain")
    return [line for line in (completed.stdout or "").splitlines() if line.strip()][:50]


def merged_into(
    *,
    repo: Path | str,
    branch: str,
    reference: str,
    runner: Runner = subprocess.run,
) -> bool:
    """Whether every commit on the branch is already reachable from the ref."""
    completed = _git(
        runner,
        Path(repo),
        "merge-base",
        "--is-ancestor",
        f"refs/heads/{valid_branch(branch)}",
        reference,
        check=False,
    )
    if completed.returncode not in (0, 1):
        detail = " ".join((completed.stderr or "").split())[:200]
        raise WorktreeError(f"cannot compare {branch} with {reference}: {detail}")
    return completed.returncode == 0


def teardown(
    *,
    state_dir: Path | str,
    path: Path | str | None = None,
    repo: Path | str | None = None,
    branch: str | None = None,
    merged_into_ref: str = "HEAD",
    force: bool = False,
    environ: Mapping[str, str] | None = None,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Prune one recorded worktree after its branch is merged.

    Refuses a dirty or unmerged worktree unless ``force`` is set, and never
    touches the branch itself: the commits outlive the directory either way.
    """
    record = lookup(
        state_dir, path=path, repo=repo, branch=branch, environ=environ
    )
    if record is None:
        raise WorktreeError("no recorded worktree matches that request")
    root = worktree_root(state_dir, environ)
    tree_path = Path(record["path"])
    trees = (root / "trees").resolve(strict=False)
    if not tree_path.is_absolute() or trees not in tree_path.resolve(strict=False).parents:
        raise WorktreeError(
            f"refusing to remove {tree_path}: outside the kit's worktree root"
        )
    repository = Path(record["repo"])
    worktree_branch = str(record["branch"])
    refusals: list[str] = []
    dirty: list[str] = []
    merged = False
    if tree_path.is_dir():
        dirty = status(path=tree_path, runner=runner)
        if dirty:
            refusals.append(f"{len(dirty)} uncommitted or untracked file(s)")
    if repository.is_dir():
        merged = merged_into(
            repo=repository,
            branch=worktree_branch,
            reference=merged_into_ref,
            runner=runner,
        )
        if not merged:
            refusals.append(f"{worktree_branch} is not merged into {merged_into_ref}")
    if refusals and not force:
        raise WorktreeError(
            "worktree kept: "
            + "; ".join(refusals)
            + ". Merge the branch, or pass --force to remove the directory anyway "
            "(the branch and its commits are never deleted)."
        )
    removed = False
    if tree_path.is_dir() and repository.is_dir():
        arguments = ["worktree", "remove"]
        if force:
            arguments.append("--force")
        arguments.append(os.fspath(tree_path))
        _git(runner, repository, *arguments)
        removed = True
    elif tree_path.is_dir():
        # The repository itself is gone; the directory is still ours to remove
        # because it is inside the kit's root and recorded.
        shutil.rmtree(tree_path)
        removed = True
    if repository.is_dir():
        _git(runner, repository, "worktree", "prune", check=False)
    record_file = _record_path(root, str(record["token"]))
    try:
        record_file.unlink()
    except FileNotFoundError:
        pass
    return {
        "removed": removed,
        "path": os.fspath(tree_path),
        "branch": worktree_branch,
        "repo": os.fspath(repository),
        "merged_into": merged_into_ref,
        "merged": merged,
        "dirty": dirty,
        "forced": bool(force and refusals),
        "shpool_id": str(record.get("shpool_id") or ""),
    }


def render(records_in: Sequence[Mapping[str, Any]]) -> str:
    """The worktree list a person reads."""
    if not records_in:
        return "  No delegated worktrees.\n"
    lines = ["  Delegated worktrees"]
    width = max(len(str(record.get("branch") or "")) for record in records_in)
    for record in records_in:
        branch = str(record.get("branch") or "")
        session = str(record.get("shpool_id") or "")
        suffix = f"  [session {session}]" if session else ""
        lines.append(f"  {branch.ljust(width)}  {record.get('path')}{suffix}")
    return "\n".join(lines) + "\n"
