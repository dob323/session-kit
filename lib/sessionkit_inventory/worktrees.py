"""Git worktree isolation for delegated workers.

A delegated worker already has a branch: the reviewed preflight names one per
workstream, and the intake entry records it before anything launches. What the
worker never had was a directory of its own, so two workers on one project
edited the same files at the same time and the branch existed only as a label.
This module materializes that already-recorded branch as a real worktree,
records it, and prunes it again once the work is merged.

Five rules the code enforces rather than documents:

* Nothing outside the kit's own worktree root is ever removed. Teardown reads
  a registry record, checks the recorded path is inside that root, and refuses
  anything else, a hand-made worktree elsewhere is the operator's, not ours.
* Nothing is written into a checkout the machine *runs*. `shared_checkout`
  answers that from the path, armed by default, and it gates creation and
  removal alike, a removal runs `git worktree remove` inside the repository,
  which is a write.
* Only this module's own registrations are ever unregistered. `git worktree
  prune` is repository-wide and takes the index, reflog and rebase state of any
  worktree it cannot reach at that moment; `forget_registration` removes the
  one entry that names the directory being given back, and nothing else.
* Dirty is refused, and "dirty" is everything work can be: uncommitted and
  untracked files, files the repository *ignores*, stashes made from the copy,
  a half-finished merge / rebase / cherry-pick / revert / bisect, and submodule
  churn. `git status --porcelain` alone answers a narrower question than the
  one being asked, and the narrow answer is indistinguishable from "empty".
* Unmerged is refused, measured against the copy's **actual HEAD** rather than
  the branch the registry recorded. Commits are never destroyed either way:
  teardown removes the worktree, never the branch, but a commit made on a
  detached HEAD is on no branch, so that promise only holds if the check looks
  at where the copy really is.
* A check that could not run has not passed. Every git question here is asked
  in order to *prove* something is safe; when git cannot answer, the reason
  goes in the refusal list next to the real ones and the copy stays. There is
  no path in this module where an error makes a removal more likely.

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
# One record per (repository, branch) the kit has ever cut, and a released one
# leaves a tombstone behind. `private_names` truncates silently past its limit,
# and `records()` is what `release`, the sweep, `sp worktree list` and the
# picker's branch labels all read -- so a registry over the limit does not fail,
# it quietly stops answering for the copies past it. The cap is high, and
# tombstones expire (:data:`TOMBSTONE_TTL_MS`), so the live count is bounded by
# what actually exists rather than by everything that ever did.
MAX_RECORDS = 4096
TOMBSTONE_TTL_MS = 30 * 24 * 60 * 60 * 1000
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


SHARED_MARKER = ".session-kit-shared"

# Where a machine keeps the software it *runs*, as opposed to where a person
# keeps a checkout to work in. A repository at or under one of these is a
# deployment: a checkout under `/srv` or `/var/www` is often the site being
# served, where the files on disk ARE the running thing. Copying one is not
# isolation, it is editing somewhere the change never takes effect, so the
# edit looks done and the site keeps serving the old page, and writing a
# branch or a worktree administrative directory into one is touching
# production.
#
# This list is armed by default, on purpose. A guarantee that has to be
# switched on is not a guarantee: the marker file and `SESSION_KIT_SHARED_REPOS`
# below are both opt-in, and on the machine this was written for neither had
# ever been set, so "nothing can reach the live checkout" was true only of a
# host that had already been configured to make it true.
SYSTEM_ROOTS = (
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
    "/opt",
    "/proc",
    "/root",
    "/run",
    "/sbin",
    "/srv",
    "/sys",
    "/usr",
    "/var",
)


# Directories a system sets aside for scratch. They sit inside system roots and
# are not software the machine runs, so they are named out, otherwise a
# repository or a worktree root in `/var/tmp` or `$XDG_RUNTIME_DIR` would be
# refused for living where scratch is supposed to live.
SCRATCH_ROOTS = ("/tmp", "/var/tmp", "/run/user", "/run/shm", "/dev/shm")


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _absolute_entries(value: str) -> list[Path]:
    """The absolute paths in a colon-separated host setting."""
    found: list[Path] = []
    for entry in (value or "").split(":"):
        candidate = entry.strip()
        if candidate.startswith("/"):
            found.append(Path(candidate).resolve(strict=False))
    return found


def system_root_of(path: Path | str) -> str:
    """The system directory this path lives under, or ``""``.

    Both the path as given and the path with its symlinks resolved are checked:
    a repository reached through a link that lands in `/srv` is still in `/srv`.
    """
    raw = Path(path)
    candidates = [raw]
    resolved = raw.resolve(strict=False)
    if resolved != raw:
        candidates.append(resolved)
    for candidate in candidates:
        if any(_within(candidate, Path(root)) for root in SCRATCH_ROOTS):
            continue
        if candidate == Path("/"):
            return "/"
        for root in SYSTEM_ROOTS:
            if _within(candidate, Path(root)):
                return root
    return ""


def shared_checkout(repo: Path | str, environ: Mapping[str, str] | None = None) -> str:
    """Why this repository must never be copied or written into, or ``""``.

    Some checkouts *are* the running thing. Editing a copy of one is not
    isolation; it is working somewhere the change never takes effect, and on a
    checkout that serves a live site, a delegated agent that thinks it is
    editing the site would be editing nothing. Nothing in this module writes
    into a repository this refuses: no branch, no worktree, no prune, and no
    removal either.

    Four answers, in the order they are asked:

    * the host says this one *is* copyable, `SESSION_KIT_COPYABLE_REPOS`, the
      one way to overrule everything below, because a rule with no exit is a
      rule somebody works around;
    * the host names it as shared, `SESSION_KIT_SHARED_REPOS`;
    * it is under a system root (:data:`SYSTEM_ROOTS`), which is a path fact
      and needs nobody to have configured anything;
    * the repository says so itself with a `.session-kit-shared` file.
    """
    environ = os.environ if environ is None else environ
    raw = Path(repo)
    resolved = raw.resolve(strict=False)
    candidates = [raw] if raw == resolved else [raw, resolved]
    for allowed in _absolute_entries(environ.get("SESSION_KIT_COPYABLE_REPOS", "")):
        if any(_within(candidate, allowed) for candidate in candidates):
            return ""
    for marked in _absolute_entries(environ.get("SESSION_KIT_SHARED_REPOS", "")):
        if any(_within(candidate, marked) for candidate in candidates):
            return f"{marked} is listed in SESSION_KIT_SHARED_REPOS"
    system = system_root_of(raw)
    if system:
        return (
            f"it is under {system}, where this machine keeps the software it "
            "runs rather than checkouts to copy; "
            "SESSION_KIT_COPYABLE_REPOS is how a host says otherwise"
        )
    marker = resolved / SHARED_MARKER
    try:
        if marker.is_file() or marker.is_symlink():
            return f"{resolved} carries a {SHARED_MARKER} marker"
    except OSError as error:
        # The one question left is whether this checkout claims to be shared,
        # and it could not be asked. Refusing costs a copy; guessing "no" costs
        # a branch written into something that might be serving live.
        return (
            f"whether {resolved} carries a {SHARED_MARKER} marker could not be "
            f"read ({error.strerror or error}), and an unanswered question is "
            "not a yes"
        )
    return ""


DEFAULT_MAX_AUTO_COPY_FILES = 20_000


def auto_copy_refusal(
    repo: Path | str,
    environ: Mapping[str, str] | None = None,
    *,
    runner: Runner = subprocess.run,
) -> str:
    """Why this repository must not be copied *without being asked*, or ``""``.

    Isolation that happens by itself has to be cheap enough to happen by
    itself. A checkout of a few hundred files costs milliseconds; one of forty
    thousand costs gigabytes of disk and seconds of wall clock inside `sp new`,
    per session, and nobody chose that at the moment it happens. So a large
    checkout is not copied automatically, the session runs in it and says so,
    naming the two ways to decide otherwise. An explicit `--worktree BRANCH` is
    a decision and is never refused for size.
    """
    environ = os.environ if environ is None else environ
    shared = shared_checkout(repo, environ)
    if shared:
        return shared
    raw = (environ.get("SESSION_KIT_AUTO_WORKTREE_MAX_FILES") or "").strip()
    try:
        limit = int(raw) if raw else DEFAULT_MAX_AUTO_COPY_FILES
    except ValueError:
        limit = DEFAULT_MAX_AUTO_COPY_FILES
    if limit <= 0:
        return ""
    completed = _git(runner, Path(repo), "ls-files", "-z", check=False)
    if completed.returncode != 0:
        return ""
    tracked = (completed.stdout or "").count("\0")
    if tracked > limit:
        return (
            f"it has {tracked:,} tracked files, more than the {limit:,} this "
            "copies without asking"
        )
    return ""


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
        detail = " ".join((completed.stderr or completed.stdout or "").split())[:300]
        raise WorktreeError(
            f"git {' '.join(arguments)} failed ({completed.returncode}): {detail}"
        )
    return completed


def repository_root(
    path: Path | str, *, runner: Runner = subprocess.run
) -> Path | None:
    """The main working tree the directory belongs to, or None when it is not one.

    None is a real answer here, the launcher asks this question exactly so it
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
        runner,
        directory,
        "rev-parse",
        "--path-format=absolute",
        "--show-toplevel",
        "--git-dir",
        "--git-common-dir",
        check=False,
    )
    if completed.returncode != 0:
        return None
    lines = [
        line.strip() for line in (completed.stdout or "").splitlines() if line.strip()
    ]
    if len(lines) != 3:
        return None
    top, git_dir, common_dir = (Path(value) for value in lines)
    if git_dir != common_dir and common_dir.name == ".git":
        return common_dir.parent
    return top


MAX_ADMIN_ENTRIES = 4096


def forget_registration(runner: Runner, repo: Path, tree_path: Path) -> str:
    """Make git forget ONE worktree registration: the one naming ``tree_path``.

    Never `git worktree prune`. That verb is repository-wide: it deletes
    ``.git/worktrees/<name>`` for *every* worktree of the repository whose
    directory it cannot reach at that moment, one on a volume that is not
    mounted, one on a share that is briefly down, one somebody renamed and
    means to move back. That directory is where a worktree keeps its index, so
    its staged-but-uncommitted content, its HEAD, its reflog and its
    half-finished rebase all go with it. Running it for this module's own
    housekeeping destroys work in checkouts this module was told never to
    touch, and it does so with `check=False`, so nobody hears about it.

    Nothing here ever needed that. It needs git to forget one registration and
    it knows exactly which one, so it reads the registrations, matches the one
    whose `gitdir` points at this directory, and removes that alone. A locked
    registration is left alone and named: git refuses to prune those, and so
    does this.

    Returns the registration name it removed, ``""`` when there was none.
    """
    completed = _git(
        runner,
        repo,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
        check=False,
    )
    if completed.returncode != 0:
        return ""
    common = (completed.stdout or "").strip()
    if not common:
        return ""
    admin_root = Path(common) / "worktrees"
    if not admin_root.is_dir():
        return ""
    wanted = tree_path.resolve(strict=False)
    try:
        entries = sorted(admin_root.iterdir())[:MAX_ADMIN_ENTRIES]
    except OSError:
        return ""
    for entry in entries:
        if not entry.is_dir() or entry.is_symlink():
            continue
        pointer = entry / "gitdir"
        try:
            recorded = pointer.read_text(encoding="utf-8", errors="strict").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if not recorded:
            continue
        # `gitdir` holds `<worktree>/.git`; the worktree is its parent.
        registered = Path(recorded).resolve(strict=False)
        if registered.name == ".git":
            registered = registered.parent
        if registered != wanted:
            continue
        if (entry / "locked").exists():
            return ""
        shutil.rmtree(entry, ignore_errors=True)
        return entry.name
    return ""


def _branch_exists(runner: Runner, repo: Path, branch: str) -> bool:
    completed = _git(
        runner,
        repo,
        "rev-parse",
        "--verify",
        "--quiet",
        f"refs/heads/{branch}",
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
    document = read_private_json(path, limit=MAX_RECORD_BYTES, label="worktree record")
    if document is None:
        return None
    return _validated_record(document, path)


def records(
    state_dir: Path | str,
    environ: Mapping[str, str] | None = None,
    *,
    include_released: bool = False,
    faults: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Every recorded worktree, newest first.

    A released copy leaves a tombstone behind so the session that ran in it can
    still be traced back to its repository. A tombstone is not a worktree, so
    it is out of every listing unless a caller asks for it by name.

    A record this cannot read is skipped so it does not hide the healthy ones,
    and its token is appended to ``faults`` when a caller supplies a list. That
    matters for one caller in particular: deciding "this session holds exactly
    one copy" from a listing that silently dropped the other one is how the
    second copy becomes permanent.
    """
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
            if faults is not None:
                faults.append(token)
            continue
        if record is None:
            continue
        if record.get("released") and not include_released:
            continue
        found.append(record)
    found.sort(key=lambda item: int(item.get("created_unix_ms") or 0), reverse=True)
    return found


def expire_tombstones(root: Path, *, now: int, ttl_ms: int = TOMBSTONE_TTL_MS) -> int:
    """Drop released records older than the tombstone lifetime.

    A tombstone exists so a closed session can still be traced back to the
    repository its copy came from. That is worth a month, not forever: the
    registry is read through a hard cap that truncates in silence, so a file
    that never expires eventually pushes a *live* copy out of every listing and
    out of the sweep. Only records marked released are touched, and only ones
    whose removal stamp is readable, an unreadable record is left where it is.
    """
    registry = root / "registry"
    if not registry.is_dir():
        return 0
    dropped = 0
    for token in private_names(registry, ".json", limit=MAX_RECORDS):
        try:
            record = read_record(root, token)
        except (WorktreeError, PrivateStoreError):
            continue
        if record is None or not record.get("released"):
            continue
        try:
            removed = int(record.get("removed_unix_ms") or 0)
        except (TypeError, ValueError):
            continue
        if removed <= 0 or now - removed < ttl_ms:
            continue
        try:
            _record_path(root, token).unlink()
        except OSError:
            continue
        dropped += 1
    return dropped


def lookup(
    state_dir: Path | str,
    *,
    path: Path | str | None = None,
    repo: Path | str | None = None,
    branch: str | None = None,
    environ: Mapping[str, str] | None = None,
    include_released: bool = False,
) -> dict[str, Any] | None:
    """One recorded worktree by its directory, or by repository and branch."""
    root = worktree_root(state_dir, environ)
    if repo is not None and branch is not None:
        record = read_record(root, token_for(Path(repo), valid_branch(branch)))
        if record is not None and record.get("released") and not include_released:
            return None
        return record
    if path is None:
        raise WorktreeError("a worktree lookup needs a path, or a repo and branch")
    wanted = os.fspath(Path(path))
    for record in records(state_dir, environ, include_released=include_released):
        if record["path"] == wanted:
            return record
    return None


def labels(
    state_dir: Path | str, environ: Mapping[str, str] | None = None
) -> dict[str, dict[str, str]]:
    """Worktree directory -> what a row can say about it.

    ``path`` is in the value as well as the key so a consumer holding one
    annotation can name the working copy itself, not only the repository it
    belongs to: the project view places a session by `repo` and then has a
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
    reads as an uncertain dispatch and costs somebody a manual reconcile, so
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
    guarded = shared_checkout(top, environ)
    if guarded:
        return {
            "repo": os.fspath(top),
            "branch": wanted,
            "path": "",
            "exists": False,
            "blocked_by": os.fspath(top),
            "reason": f"nothing is copied out of {top}: {guarded}",
        }
    root = worktree_root(state_dir, environ)
    destination = (
        root / "trees" / f"{slug(top.name)}-{token_for(top, wanted)[:8]}" / slug(wanted)
    )
    occupied = _checked_out_at(runner, top, wanted)
    blocked = occupied if occupied is not None and Path(occupied) != destination else ""
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
    auto: bool = False,
    origin: str = "",
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
    # Checked before a single git command runs against it, so a shared checkout
    # never gains a branch, a worktree administrative directory, or a prune.
    shared = shared_checkout(top, environ)
    if shared:
        raise WorktreeError(
            f"{top} is a shared checkout ({shared}), so nothing is copied out "
            "of it and nothing is written into it"
        )
    root = worktree_root(state_dir, environ)
    # The other direction of the same rule: a working copy is never *built*
    # where the machine keeps the software it runs, however the root was
    # configured. Checked here rather than in `worktree_root` so that listing
    # and labelling a badly configured host still work, only creation stops.
    landing = system_root_of(root)
    if landing:
        raise WorktreeError(
            f"refusing to build a working copy under {landing}: the worktree "
            f"root is {root}, and that is where this machine keeps software it "
            "runs. Point SESSION_KIT_WORKTREE_ROOT somewhere of your own."
        )
    private_directory(root, label="worktree root")
    token = token_for(top, wanted_branch)
    existing = read_record(root, token)
    if existing is not None:
        current = Path(existing["path"])
        if current.is_dir() and not current.is_symlink():
            return {**existing, "created": False}
        # The directory is gone from under a live record: let git forget that
        # one registration, then fall through and build it again at the
        # recorded path. Scoped on purpose, see `forget_registration`.
        forget_registration(runner, top, current)
    trees = _trees_dir(root)
    # The repository's own name is not unique on a machine: two projects on
    # this host are both called `app`. The token disambiguates them, so the
    # same branch name in two repositories cannot land on one directory and
    # refuse the second launch.
    destination = trees / f"{slug(top.name)}-{token[:8]}" / slug(wanted_branch)
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
        # Who chose this working copy, and therefore who may remove it again.
        # `auto` means the kit cut the branch itself because the session is
        # delegated work; only those are released when the session closes. A
        # branch a person named is theirs, and so is every worktree that
        # predates this record field: absent means no.
        "auto": bool(auto),
        "origin": str(origin or ""),
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


def status(*, path: Path | str, runner: Runner = subprocess.run) -> list[str]:
    """Uncommitted, untracked and ignored entries in one worktree.

    ``--ignored`` is not optional here. A fresh worktree starts with no ignored
    files at all, so every ignored file in one was made by whoever worked in
    it: a log, a screenshot, a report, a ``.env``, a one-off script. Asking git
    the default question returns nothing for those, and the copy was removed
    with them in it. ``--untracked-files=all`` counts inside an untracked
    directory rather than reporting the directory once, and submodules are not
    ignored, so a repository that hides its own submodule churn cannot hide it
    here either.
    """
    completed = _git(
        runner,
        Path(path),
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--ignored=matching",
        "--ignore-submodules=none",
    )
    return [line for line in (completed.stdout or "").splitlines() if line.strip()][:50]


# A worktree's own git directory holds the state of anything half-finished in
# it. `git status --porcelain` is empty in the middle of every one of these.
WORK_MARKERS = (
    ("MERGE_HEAD", "a merge is in progress"),
    ("rebase-merge", "a rebase is in progress"),
    ("rebase-apply", "a rebase or patch series is in progress"),
    ("CHERRY_PICK_HEAD", "a cherry-pick is in progress"),
    ("REVERT_HEAD", "a revert is in progress"),
    ("BISECT_LOG", "a bisect is in progress"),
    ("sequencer", "an unfinished sequence of commits is in progress"),
)


def head_of(*, path: Path | str, runner: Runner = subprocess.run) -> dict[str, Any]:
    """What the copy is actually on: its commit, and its branch if it has one.

    The registry records the branch a worktree was cut on. It is not evidence
    of where the worktree is *now*: a worker can detach HEAD, or check out
    something else entirely, and the recorded ref never moves. Every decision
    about removing a directory has to be made against this, not against the
    record.

    Each question is asked without ``check``, because a failure here is an
    answer the caller has to act on rather than an exception to unwind on, but
    it is the answer *"nobody knows"*, and it comes back in ``unreadable`` so
    that no caller can mistake an empty string for "there is nothing there".
    Every guarantee in this module rests on one of these three values; a git
    that could not answer switches off exactly the check that was protecting
    the work.
    """
    completed = _git(runner, Path(path), "rev-parse", "HEAD", check=False)
    commit = (completed.stdout or "").strip() if completed.returncode == 0 else ""
    named = _git(
        runner, Path(path), "symbolic-ref", "--quiet", "--short", "HEAD", check=False
    )
    # A failure here is normal and means exactly one thing: HEAD is detached.
    branch = (named.stdout or "").strip() if named.returncode == 0 else ""
    top = _git(runner, Path(path), "rev-parse", "--absolute-git-dir", check=False)
    git_dir = (top.stdout or "").strip() if top.returncode == 0 else ""
    unreadable: list[str] = []
    if not commit:
        detail = " ".join((completed.stderr or "").split())[:160]
        unreadable.append(
            "git could not say which commit this copy is on"
            + (f" ({detail})" if detail else "")
            + ", so nothing here can prove its commits exist anywhere else"
        )
    if not git_dir:
        detail = " ".join((top.stderr or "").split())[:160]
        unreadable.append(
            "git could not say where this copy's own git directory is"
            + (f" ({detail})" if detail else "")
            + ", so a half-finished merge or rebase in it would be invisible"
        )
    return {
        "commit": commit,
        "branch": branch,
        "detached": bool(commit) and not branch,
        "git_dir": git_dir,
        "unreadable": unreadable,
    }


def in_progress(git_dir: str | None) -> list[str]:
    """Half-finished git operations whose state lives in the worktree."""
    if not git_dir:
        return []
    root = Path(git_dir)
    return [reason for name, reason in WORK_MARKERS if (root / name).exists()]


def stashes_for(
    *,
    path: Path | str,
    branches: Sequence[str] = (),
    head_commit: str = "",
    detached: bool = False,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Stash entries this copy could have made.

    A stash is somebody's work by definition, and `git status` cannot see one.
    The entries live in the repository rather than in the worktree, so they
    have to be attributed to it. Three ways, and any one is enough to keep the
    directory:

    * **the entry's first parent is the commit this copy is standing on.** The
      only definitive answer, and the only one that works for a stash pushed
      from a detached HEAD, git records those as ``On (no branch): …``, which
      names nothing at all.
    * **the subject names one of this copy's branches**, compared with case
      folded on both sides. Git writes the branch as spelled, so a copy on
      ``Feature/Work`` gets ``On Feature/Work:``; a search that lower-cases only
      the line it is reading finds nothing.
    * **the copy is detached and the subject says ``(no branch)``.**

    Over-matching is the safe direction here and is deliberate: a stash made in
    the main checkout at the same commit keeps the copy, which costs a
    directory. Under-matching costs the stash.

    A `git stash list` that fails comes back in ``unreadable``. "The question
    could not be asked" is never rendered as "the answer is no".
    """
    completed = _git(
        runner,
        Path(path),
        "stash",
        "list",
        "--format=%gd%x09%P%x09%gs",
        check=False,
    )
    if completed.returncode != 0:
        detail = " ".join((completed.stderr or "").split())[:160]
        return {
            "entries": [],
            "unreadable": (
                "the stash list could not be read"
                + (f" ({detail})" if detail else "")
                + ", so a stashed change would be invisible"
            ),
        }
    markers: list[str] = []
    for name in branches:
        folded = (name or "").strip().casefold()
        if folded:
            markers.extend((f"on {folded}:", f"on {folded} "))
    found: list[str] = []
    for line in (completed.stdout or "").splitlines():
        fields = line.split("\t")
        if len(fields) < 3:
            continue
        name = fields[0].strip()
        parents = fields[1].split()
        subject = "\t".join(fields[2:]).strip()
        base = parents[0] if parents else ""
        folded = subject.casefold()
        if (
            (head_commit and base == head_commit)
            or any(marker in folded for marker in markers)
            or (detached and "(no branch)" in folded)
        ):
            found.append(f"{name} {subject}".strip())
        if len(found) >= 20:
            break
    return {"entries": found, "unreadable": ""}


def inspect_work(
    *,
    path: Path | str,
    repo: Path | str,
    branch: str,
    reference: str,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Everything in a working copy that a person would call their work.

    One place, so no caller can decide a copy is empty by asking a narrower
    question than the next caller does. It answers with facts and a list of
    plain-language reasons to keep the directory; an empty list is the only
    thing that permits a removal.

    Two kinds of reason go in that list and both keep the copy. ``refusals``
    that name work, a file, a commit, a stash, and ``unreadable`` ones that
    name a question git could not answer. The second kind matters as much as
    the first: every check here is "prove the work is safe", so a check that
    could not run has not passed, and reading its empty result as "there is
    nothing there" is how a copy with work in it is removed by a guard that was
    switched off by an error nobody saw.
    """
    tree = Path(path)
    repository = Path(repo)
    refusals: list[str] = []
    blind: list[str] = []
    # Reasons `--force` does not cover, collected where each one is decided
    # rather than matched back out of the sentence it produced. A substring
    # search over rendered text is one reworded message away from letting a
    # force through the thing it was written to stop.
    unforceable: list[str] = []
    facts: dict[str, Any] = {
        "head": {},
        "dirty": [],
        "ignored": [],
        "stashes": [],
        "in_progress": [],
        "unreadable": [],
        "unforceable": [],
        "merged": False,
        "identity": "",
    }
    if not tree.is_dir():
        return {
            **facts,
            "refusals": refusals,
            "present": False,
        }
    head = head_of(path=tree, runner=runner)
    facts["head"] = head
    blind.extend(head.get("unreadable") or [])
    # Is the checkout at that path still the one the record describes? A kit
    # worktree removed outside the kit leaves the record behind, and a
    # directory made there afterwards is somebody else's checkout, not ours.
    # The comparison is component-wise: a plain string prefix would read
    # `<repo>/.git/worktrees-elsewhere/x` as being inside `<repo>/.git/worktrees`.
    expected = (repository / ".git" / "worktrees").resolve(strict=False)
    if head["git_dir"] and not _within(
        Path(head["git_dir"]).resolve(strict=False), expected
    ):
        reason = (
            "the checkout there is not the one recorded, so it is not ours to remove"
        )
        refusals.append(reason)
        unforceable.append(reason)
        facts["identity"] = "foreign"
    elif branch and head["branch"] and head["branch"] != branch:
        reason = f"it is on {head['branch']} now, not the recorded {branch}"
        refusals.append(reason)
        unforceable.append(reason)
        facts["identity"] = "moved"
    entries = status(path=tree, runner=runner)
    dirty = [line for line in entries if not line.startswith("!!")]
    ignored = [line for line in entries if line.startswith("!!")]
    facts["dirty"] = dirty
    facts["ignored"] = ignored
    if dirty:
        refusals.append(f"{len(dirty)} uncommitted or untracked file(s)")
    if ignored:
        # Named separately: "untracked" reads like scratch, and this is where
        # an agent's report, log, screenshot set or .env actually lives.
        refusals.append(
            f"{len(ignored)} file(s) the repository ignores but somebody made"
        )
    facts["in_progress"] = in_progress(head.get("git_dir"))
    refusals.extend(facts["in_progress"])
    if facts["in_progress"]:
        # The conflict resolution in the tree and the replay state in
        # `.git/worktrees/<n>/rebase-merge` are both destroyed by
        # `git worktree remove --force`, and neither is on a branch, so nothing
        # can bring them back. `--force` is a decision about a directory; this
        # is not one. The way out is one command, and it is named.
        unforceable.extend(
            f"{reason} (finish it or undo it in the copy first: "
            f"git -C {tree} rebase --abort, merge --abort, cherry-pick --abort, "
            "revert --abort or bisect reset)"
            for reason in facts["in_progress"]
        )
    stash = stashes_for(
        path=tree,
        branches=(head.get("branch") or "", branch),
        head_commit=str(head.get("commit") or ""),
        detached=bool(head.get("detached")),
        runner=runner,
    )
    facts["stashes"] = stash["entries"]
    if stash["entries"]:
        refusals.append(f"{len(stash['entries'])} stashed change(s) from this copy")
    if stash["unreadable"]:
        blind.append(stash["unreadable"])
    # The commit the copy is actually standing on has to be in the reference,
    # whether it is on the recorded branch, on another branch, or on no branch
    # at all. Asking only whether the recorded ref is merged says "merged" for
    # a worker who detached HEAD and committed -- and removing the worktree
    # then deletes that commit's only reflog with it.
    if not repository.is_dir():
        blind.append(
            f"the repository this copy came from is gone ({repository}), so "
            "nothing here can prove its commits exist anywhere else"
        )
    elif head.get("commit"):
        completed = _git(
            runner,
            repository,
            "merge-base",
            "--is-ancestor",
            head["commit"],
            reference,
            check=False,
        )
        if completed.returncode not in (0, 1):
            detail = " ".join((completed.stderr or "").split())[:200]
            blind.append(
                f"its commits could not be compared with {reference}: {detail}"
            )
        else:
            facts["merged"] = completed.returncode == 0
            if not facts["merged"]:
                where = head["branch"] or f"the commit it is on ({head['commit'][:12]})"
                refusals.append(f"{where} is not merged into {reference}")
    # Nothing but this copy's own reflog reaches a commit that is on no branch,
    # and `git worktree remove` deletes that reflog with the directory. So the
    # rule is not "detached and *known* to be unmerged", it is detached and
    # not PROVED merged. A `merge-base` that errored, a reference that does not
    # exist, a HEAD git could not read: each of those leaves `merged` false for
    # a reason that has nothing to do with the work being safe, and each of
    # them used to be forceable while the one case that was spelled out was not.
    if not facts["merged"] and (head.get("detached") or not head.get("commit")):
        unforceable.append(
            "its HEAD is not on a branch, and nothing here could prove its "
            f"commits are already in {reference}; removing the copy would take "
            "the only reflog that reaches them"
        )
    # Last, so a person reads what is *in* the copy before what could not be
    # checked about it. Most of these stay forceable: a failed stash list, a
    # missing repository, a comparison that errored on a copy whose branch does
    # hold its commits -- a person who has read the reason can still remove the
    # directory. The exception is above: anything that leaves "is there a
    # commit here that only this copy holds" unanswered.
    facts["unreadable"] = blind
    facts["unforceable"] = unforceable
    refusals.extend(blind)
    return {**facts, "refusals": refusals, "present": True}


# `merged_into()` used to live here: it asked whether `refs/heads/<recorded
# branch>` was an ancestor of the reference. That is the question that removed a
# commit made on a detached HEAD, the recorded branch had not moved, so it was
# an ancestor, so the copy read as merged. `inspect_work` asks about the copy's
# actual HEAD instead. The old helper is deleted rather than left dead beside
# the new one: a function that answers a nearly-identical question with the
# wrong evidence is a trap for whoever edits this next.


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
    proc: Path | str = Path("/proc"),
    exclude_pids: Sequence[int] = (),
) -> dict[str, Any]:
    """Prune one recorded worktree after its branch is merged.

    Refuses a dirty or unmerged worktree unless ``force`` is set, and never
    touches the branch itself: the commits outlive the directory either way.
    A working copy somebody is still standing in is refused whatever ``force``
    says. That is not a decision about unsaved work; it is pulling the floor
    out from under a live session.
    """
    record = lookup(state_dir, path=path, repo=repo, branch=branch, environ=environ)
    if record is None:
        raise WorktreeError("no recorded worktree matches that request")
    root = worktree_root(state_dir, environ)
    tree_path = Path(record["path"])
    trees = (root / "trees").resolve(strict=False)
    if (
        not tree_path.is_absolute()
        or trees not in tree_path.resolve(strict=False).parents
    ):
        raise WorktreeError(
            f"refusing to remove {tree_path}: outside the kit's worktree root"
        )
    # The second half of "nothing reaches a checkout like that". Creation is
    # refused in `materialize`; removal has its own door, because a removal
    # runs `git worktree remove` *in the repository*, a write into the very
    # thing the creation guard exists to keep out of. A
    # record naming a protected repository is refused before either runs, and
    # `--force` is not a way past it: the answer is not about this copy.
    guarded = shared_checkout(Path(record["repo"]), environ)
    if guarded:
        raise WorktreeError(
            f"refusing to touch {record['repo']}: {guarded}. Nothing is written "
            "into a checkout like that, removal included."
        )
    scan = busy_scan(
        tree_path, proc=proc, exclude=[*_ancestor_pids(Path(proc)), *exclude_pids]
    )
    occupants = scan["pids"]
    if occupants:
        listed = ", ".join(str(pid) for pid in occupants[:5])
        raise WorktreeError(
            f"refusing to remove {tree_path}: pid(s) {listed} are still working "
            "in it. Close that session first."
        )
    if scan["unreadable"]:
        raise WorktreeError(f"refusing to remove {tree_path}: {scan['unreadable']}")
    repository = Path(record["repo"])
    worktree_branch = str(record["branch"])
    work = inspect_work(
        path=tree_path,
        repo=repository,
        branch=worktree_branch,
        reference=merged_into_ref,
        runner=runner,
    )
    refusals = list(work["refusals"])
    dirty = list(work["dirty"]) + list(work["ignored"])
    merged = bool(work["merged"])
    # Some of these are not decisions about unsaved work, and `--force` does
    # not cover them: a directory that is not the checkout we recorded is
    # somebody else's, a commit on no branch cannot be re-made from the branch
    # afterwards, and a half-finished rebase lives nowhere but here. The list
    # comes from `inspect_work`, which marks each one where it decides it
    # rather than being matched back out of the sentence it produced.
    unforceable = list(work.get("unforceable") or [])
    if unforceable:
        others = [reason for reason in refusals if reason not in unforceable]
        raise WorktreeError(
            "worktree kept: "
            + "; ".join(unforceable)
            + (f"; also {'; '.join(others)}" if others else "")
            + ". This is refused whatever --force says, because nothing else "
            "holds that work."
        )
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
        # `git worktree remove` already dropped the registration in the normal
        # case; this covers the one it left behind. Scoped to this copy's own
        # entry, a repository-wide prune here would delete the administrative
        # directory, and therefore the index and rebase state, of every other
        # worktree of this repository that happens to be unreachable right now.
        forget_registration(runner, repository, tree_path)
    # The record becomes a tombstone rather than disappearing. A session that
    # ran in this copy is still in the closed list, and `sp recover` hands its
    # recorded directory back to the restore path -- which would refuse a
    # directory that no longer exists. The tombstone is how that path finds the
    # repository the copy came from instead of dead-ending.
    stamp = now_unix_ms()
    tombstone = {
        **{key: value for key, value in record.items() if key != "removed_unix_ms"},
        "released": True,
        "removed_unix_ms": stamp,
    }
    write_private_json(_record_path(root, str(record["token"])), tombstone)
    expire_tombstones(root, now=stamp)
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


# ---- release: the copy goes when the session that got it closes ---------
#
# `teardown` is the operator's verb and raises at them. `release` is what a
# close calls, so it answers with a verdict instead: a close must never fail
# because of a directory, and a directory must never be removed because a
# close was in a hurry. Four things stop it, each of them said out loud:
#
#   * no record, or a record this did not create   -> not ours to remove
#   * anything outside the kit's own worktree root -> never touched
#   * a live process still working in it           -> kept, with the pid
#   * uncommitted or unmerged work                 -> kept, with the reason
KEPT = "kept"
REMOVED = "removed"
NONE = "none"
# One session, more than one copy: each is answered for, in `verdicts`.
MANY = "many"


def _ancestor_pids(proc: Path = Path("/proc")) -> set[int]:
    """This process and everything that started it.

    A session closing from the inside (`bye`, or a provider that exited)
    runs this code *in* the working copy being released, so its own shell
    would otherwise count as somebody still working in it.
    """
    found: set[int] = set()
    pid = os.getpid()
    for _ in range(64):
        if pid <= 0 or pid in found:
            break
        found.add(pid)
        try:
            text = (proc / str(pid) / "status").read_text(encoding="utf-8")
        except (OSError, ValueError):
            break
        parent = 0
        for line in text.splitlines():
            if line.startswith("PPid:"):
                try:
                    parent = int(line.split()[1])
                except (IndexError, ValueError):
                    parent = 0
                break
        pid = parent
    return found


def busy_scan(
    path: Path | str,
    *,
    proc: Path | str = Path("/proc"),
    exclude: Sequence[int] = (),
) -> dict[str, Any]:
    """Live processes whose working directory is in ``path``, and what was hidden.

    Evidence, not bookkeeping: a session that is still standing in the
    directory shows up here whether or not any record says it owns it.

    The process table is not always readable. ``hidepid``, a restricted
    container, a `/proc` that is not mounted, each of those produces an empty
    list that is indistinguishable from "nobody is working in it", and that
    list is the *only* thing standing between a live session and having the
    floor pulled out from under it. So the scan reports what it could not read:
    ``unreadable`` non-empty means this answer is not evidence of anything.

    One process whose own `cwd` link cannot be read is *not* reported. On any
    ordinary machine most of the process table belongs to somebody else and is
    unreadable by design; treating that as doubt would keep every copy forever
    and turn the whole feature off. The line drawn here is between "some of the
    table is not mine to read", which is normal, and "the table could not be
    read at all", which is the case that silently answers "nobody is here".
    """
    root = Path(proc)
    wanted = Path(path).resolve(strict=False)
    skip = set(exclude)
    found: list[int] = []
    try:
        entries = sorted(
            (entry for entry in root.iterdir() if entry.name.isdigit()),
            key=lambda entry: int(entry.name),
        )
    except OSError as error:
        return {
            "pids": [],
            "unreadable": (
                f"the process table at {root} could not be read "
                f"({error.strerror or error}), so nothing here can say whether "
                "a session is still working in it"
            ),
        }
    for entry in entries:
        pid = int(entry.name)
        if pid in skip:
            continue
        try:
            cwd = (entry / "cwd").resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            # Not ours to read, or gone between listing and reading. Either
            # way it is not evidence of use.
            continue
        if cwd == wanted or wanted in cwd.parents:
            found.append(pid)
    return {"pids": found, "unreadable": ""}


def busy_pids(
    path: Path | str,
    *,
    proc: Path | str = Path("/proc"),
    exclude: Sequence[int] = (),
) -> list[int]:
    """The pids from :func:`busy_scan`, for callers that only want the list."""
    return list(busy_scan(path, proc=proc, exclude=exclude)["pids"])


def _verdict(
    action: str, reason: str, record: Mapping[str, Any] | None = None, **extra: Any
) -> dict[str, Any]:
    verdict = {
        "action": action,
        "reason": reason,
        "path": str((record or {}).get("path") or extra.pop("path", "")),
        "branch": str((record or {}).get("branch") or ""),
        "shpool_id": str((record or {}).get("shpool_id") or ""),
    }
    verdict.update(extra)
    return verdict


def release(
    state_dir: Path | str,
    *,
    shpool_id: str | None = None,
    path: Path | str | None = None,
    merged_into_ref: str = "HEAD",
    environ: Mapping[str, str] | None = None,
    runner: Runner = subprocess.run,
    proc: Path | str = Path("/proc"),
    exclude_pids: Sequence[int] = (),
) -> dict[str, Any]:
    """Give back the working copy a closing session was given.

    Never raises: every outcome is a verdict the caller prints. Only worktrees
    this kit cut for a delegated session are candidates, so the ones already on
    the machine when this shipped are left exactly where they are.
    """
    record: Mapping[str, Any] | None = None
    if shpool_id:
        wanted = str(shpool_id).strip()
        faults: list[str] = []
        owned = [
            item
            for item in records(state_dir, environ, faults=faults)
            if str(item.get("shpool_id") or "") == wanted
        ]
        if faults:
            # "This session holds exactly one copy" cannot be concluded from a
            # listing that dropped a record it could not read. The one below
            # would be released and the unreadable one forgotten, which is the
            # permanent-directory failure the MANY branch exists to prevent.
            return _verdict(
                KEPT,
                f"{len(faults)} worktree record(s) in the registry could not be "
                "read, so nothing here can say how many copies this session "
                "holds; `sp worktree list` shows the ones that are readable",
                None,
                faults=faults,
            )
        if len(owned) > 1:
            # A session can hold more than one copy (a retried launch on a new
            # branch). Releasing the newest and forgetting the rest is how a
            # directory becomes permanent, so each one is answered for.
            return _verdict(
                MANY,
                f"{len(owned)} working copies are bound to that session",
                None,
                verdicts=[
                    release(
                        state_dir,
                        path=item["path"],
                        merged_into_ref=merged_into_ref,
                        environ=environ,
                        runner=runner,
                        proc=proc,
                        exclude_pids=exclude_pids,
                    )
                    for item in owned
                ],
            )
        record = owned[0] if owned else None
    if record is None and path is not None:
        try:
            record = lookup(state_dir, path=path, environ=environ)
        except WorktreeError as error:
            return _verdict(NONE, str(error), path=os.fspath(Path(path)))
    if record is None:
        return _verdict(
            NONE,
            "no worktree record covers that session",
            path=os.fspath(Path(path)) if path is not None else "",
        )
    if not record.get("auto"):
        return _verdict(
            KEPT,
            "that working copy was asked for by name, so it stays; "
            "remove it with sp teardown",
            record,
        )
    tree_path = Path(str(record["path"]))
    try:
        root = worktree_root(state_dir, environ)
    except WorktreeError as error:
        return _verdict(KEPT, str(error), record)
    trees = (root / "trees").resolve(strict=False)
    if (
        not tree_path.is_absolute()
        or trees not in tree_path.resolve(strict=False).parents
    ):
        return _verdict(
            KEPT, "it is outside the kit's worktree root, so it is not ours", record
        )
    scan = busy_scan(
        tree_path,
        proc=proc,
        exclude=[*_ancestor_pids(Path(proc)), *exclude_pids],
    )
    busy = scan["pids"]
    if busy:
        listed = ", ".join(str(pid) for pid in busy[:5])
        return _verdict(
            KEPT,
            f"something is still working in it (pid {listed})",
            record,
            busy=busy,
        )
    if scan["unreadable"]:
        return _verdict(KEPT, scan["unreadable"], record)
    try:
        result = teardown(
            state_dir=state_dir,
            path=tree_path,
            merged_into_ref=merged_into_ref,
            force=False,
            environ=environ,
            runner=runner,
            # The same `/proc` and the same exemptions this call was given.
            # Dropping them made `teardown` re-run the occupancy check against
            # the real process table with the closing session's own pids back
            # in it -- and made every test that hands `release` a fake `/proc`
            # prove nothing about the check it was written for.
            proc=proc,
            exclude_pids=exclude_pids,
        )
    except WorktreeError as error:
        return _verdict(KEPT, str(error), record)
    return _verdict(
        REMOVED,
        f"merged into {merged_into_ref} and clean",
        record,
        removed=bool(result.get("removed")),
    )


def release_idle(
    state_dir: Path | str,
    active_shpool_ids: Sequence[str] | None,
    *,
    merged_into_ref: str = "HEAD",
    environ: Mapping[str, str] | None = None,
    runner: Runner = subprocess.run,
    proc: Path | str = Path("/proc"),
) -> list[dict[str, Any]]:
    """Release every automatic worktree whose session is provably gone.

    A close that lands while the worker is still writing keeps the directory
    on purpose. Without this sweep that decision would be permanent, so the
    same check runs again later, but only ever against a list of what is
    alive that the caller actually supplies.

    Two rules make this safe to schedule:

    * **No list, no sweep.** ``None`` is refused rather than read as "nothing
      is alive". A verb that deletes must never treat missing knowledge as
      permission, and an empty list is only accepted when the caller means it.
    * **No owner, no removal.** A copy whose session was never recorded cannot
      be matched against the live list at all, so nothing here can prove it is
      idle. It is reported and left alone, however old it is. Age is not
      ownership, and a bind that failed leaves a *running* session in exactly
      that state.
    """
    if active_shpool_ids is None:
        raise WorktreeError(
            "a sweep needs the list of sessions that are alive; refusing to "
            "treat 'no list' as 'nothing is alive'"
        )
    live = {str(value).strip() for value in active_shpool_ids if str(value).strip()}
    verdicts: list[dict[str, Any]] = []
    for record in records(state_dir, environ):
        if not record.get("auto"):
            continue
        owner = str(record.get("shpool_id") or "")
        if owner and owner in live:
            continue
        if not owner:
            verdicts.append(
                _verdict(
                    KEPT,
                    "no session is recorded for this copy, so nothing here can "
                    "prove it is idle; remove it with sp teardown when you know "
                    "it is finished",
                    record,
                )
            )
            continue
        verdicts.append(
            release(
                state_dir,
                path=record["path"],
                merged_into_ref=merged_into_ref,
                environ=environ,
                runner=runner,
                proc=proc,
            )
        )
    return verdicts


def render_verdict(verdict: Mapping[str, Any]) -> str:
    """One line a person reads after a close."""
    action = str(verdict.get("action") or NONE)
    if action == NONE:
        return ""
    if action == MANY:
        return "".join(render_verdict(item) for item in verdict.get("verdicts") or [])
    branch = str(verdict.get("branch") or "")
    path = str(verdict.get("path") or "")
    if action == REMOVED:
        return f"Removed the {branch} working copy at {path}.\n"
    if not branch and not path:
        # A keep that is about the registry rather than about one copy: there
        # is no branch and no directory to name, and "Kept the  working copy
        # at :" reads like a bug on top of the thing it is reporting.
        return f"Kept the working copies of this session: {verdict.get('reason')}\n"
    return f"Kept the {branch} working copy at {path}: {verdict.get('reason')}\n"


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
