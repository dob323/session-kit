"""What is going on in a project, gathered from every record that mentions it.

Entering a project should answer the question a person actually has — "where
did I leave this?" — and the answer is keyed on the project root: a session
belongs to a project when its working directory is inside that root. The one
membership rule lives in :mod:`lib.sessionkit_projects.identity`, so the
picker and ``sp new`` agree about what is in a project.

Everything here is a pure function over records the caller supplies. The
inventory snapshot is read by the command line in
:mod:`lib.sessionkit_projects.cli`, which keeps this module testable without
a live provider or a running daemon.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from .identity import Project, _within

MAX_SESSIONS = 200

# The fields a caller may see about a session. Copying a whitelist rather than
# the whole row keeps a context view from growing new fields, including
# sensitive ones, whenever the inventory row grows one.
SESSION_FIELDS = (
    "shpool_id",
    "display_shpool_id",
    "terminal_number",
    "provider",
    "display_provider",
    "cwd",
    "title",
    "display_title",
    "display_color",
    "agent_status",
    "availability",
    "needs_you",
    "active_subagent_count",
    "started_at_unix_ms",
    # Set by the worktree registry when a row's directory is a materialised
    # worktree: {"branch", "repo"}, where repo is the main repository, the
    # same path this module calls a group root.
    "worktree",
)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _worktree_repo(row: Mapping[str, Any]) -> str | None:
    """The main repository a session's worktree was cut from, if recorded.

    A materialised worktree is rarely on the host's shortcut list and need not
    carry a manifest, so path resolution alone can leave its sessions
    unplaceable. The worktree registry already knows the answer, and records it
    as the same absolute repository path this module groups by.
    """
    label = row.get("worktree")
    if not isinstance(label, Mapping):
        return None
    repo = _text(label.get("repo"))
    return repo if repo and repo.startswith("/") else None


def _member_roots(project: Project, group: Sequence[Project] | None) -> list[str]:
    """The directories that count as this project.

    A linked worktree is the same project as the repository it was cut from,
    so work in a worktree belongs to the project rather than disappearing
    into an unnamed directory.
    """
    roots = [project.root]
    for member in group or ():
        if member.group == project.group and member.root not in roots:
            roots.append(member.root)
    return roots


def _belongs(directory: str | None, roots: Iterable[str]) -> str | None:
    if not directory:
        return None
    for root in roots:
        if _within(directory, root):
            return root
    return None


def _project_sessions(
    sessions: Iterable[Mapping[str, Any]], roots: Sequence[str], group_root: str
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for row in sessions:
        if not isinstance(row, Mapping):
            continue
        matched = _belongs(_text(row.get("cwd")), roots)
        if matched is None and _worktree_repo(row) == group_root:
            matched = group_root
        if matched is None:
            continue
        entry = {field: row.get(field) for field in SESSION_FIELDS if field in row}
        entry["project_root"] = matched
        found.append(entry)
        if len(found) >= MAX_SESSIONS:
            break
    return found


def project_context(
    project: Project,
    *,
    sessions: Iterable[Mapping[str, Any]] = (),
    group: Sequence[Project] | None = None,
    unavailable: Sequence[str] = (),
) -> dict[str, Any]:
    """Everything the kit knows about one project, in one record.

    ``unavailable`` names the stores the caller could not read, so a context
    with no sessions because the inventory is missing never reads the same as
    a context with no sessions because nothing is running.
    """
    roots = _member_roots(project, group)
    found_sessions = _project_sessions(sessions, roots, project.group)
    manifest = project.manifest if isinstance(project.manifest, Mapping) else {}
    team = manifest.get("team", [])
    return {
        "project": project.to_dict(),
        "roots": roots,
        "sessions": found_sessions,
        "team": [dict(role) for role in team] if isinstance(team, list) else [],
        "counts": {
            "sessions": len(found_sessions),
            "worktrees": max(0, len(roots) - 1),
            "team_roles": len(team) if isinstance(team, list) else 0,
        },
        "unavailable": list(unavailable),
    }


def group_sessions_by_project(
    sessions: Iterable[Mapping[str, Any]],
    assignments: Mapping[str, Project | None],
    *,
    projects: Sequence[Project] | None = None,
) -> dict[str, Any]:
    """Bucket a session list by project for a grouped view.

    ``assignments`` comes from :meth:`Resolver.assign`, so one pass over the
    shortcut table and the manifests serves the whole list. Sessions whose
    directory belongs to no project are kept together under ``ungrouped``
    rather than dropped — a session the picker cannot place must still be
    reachable.

    ``projects`` is the host's project list, used to place a worktree session
    by its recorded repository. It must come from the host rather than from
    ``assignments``: a project whose work has been fanned out into worktrees
    may have no session in its own directory at all, which is precisely the
    delegated case this placement exists for.
    """
    groups: dict[str, dict[str, Any]] = {}
    ungrouped: list[dict[str, Any]] = []
    known = {
        project.group: project
        for project in assignments.values()
        if project is not None
    }
    for listed in projects or ():
        known.setdefault(listed.group, listed)
    for row in sessions:
        if not isinstance(row, Mapping):
            continue
        directory = _text(row.get("cwd"))
        project = assignments.get(directory) if directory else None
        entry = {field: row.get(field) for field in SESSION_FIELDS if field in row}
        by_worktree = False
        if project is None:
            # A worktree of a known project, placed by the registry rather than
            # by its path. Its own root is not recorded on the row, so the row
            # is attributed to the project it belongs to, not to a root this
            # cannot name.
            repo = _worktree_repo(row)
            project = known.get(repo) if repo else None
            by_worktree = project is not None
        if project is None:
            ungrouped.append(entry)
            continue
        bucket = groups.setdefault(
            project.group,
            {
                "group_root": project.group,
                "name": project.name,
                "roots": [],
                "sessions": [],
            },
        )
        if project.root == project.group:
            bucket["name"] = project.name
        if not by_worktree and project.root not in bucket["roots"]:
            bucket["roots"].append(project.root)
        entry["project_root"] = project.group if by_worktree else project.root
        entry["project_name"] = project.name
        bucket["sessions"].append(entry)
    for bucket in groups.values():
        bucket["roots"].sort()
    return {
        "groups": sorted(
            groups.values(),
            key=lambda item: (str(item["name"]), str(item["group_root"])),
        ),
        "ungrouped": ungrouped,
    }
