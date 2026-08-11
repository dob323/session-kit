"""What is going on in a project, gathered from every record that mentions it.

Entering a project should answer the question a person actually has — "where
did I leave this?" — and until now nothing could, because the two halves of
the answer lived in stores that did not know about each other: live sessions
in the inventory, delegated work in the supervisor's intake spool, and no
shared key between them.

The shared key is the project root. A session belongs to a project when its
working directory is inside that root; an intake belongs when the directory
it arrived from is. Both use the one membership rule in
:mod:`lib.sessionkit_projects.identity`, so the picker, the supervisor, and
``sp new`` agree about what is in a project.

Everything here is a pure function over records the caller supplies. The
inventory snapshot and the intake spool are read by the command line in
:mod:`lib.sessionkit_projects.cli`, which keeps this module testable without
a live provider, a running daemon, or a spool on disk.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from .identity import Project, _within

MAX_SESSIONS = 200
MAX_INTAKES = 200

# The fields a caller may see about a session. Copying a whitelist rather than
# the whole row keeps a context view from growing new fields — including
# sensitive ones — whenever the inventory row grows one.
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
    # worktree: {"branch", "repo"}, where repo is the main repository — the
    # same path this module calls a group root.
    "worktree",
)
INTAKE_FIELDS = (
    "msg_id",
    "state",
    "summary",
    "source_cwd",
    "source_title",
    "received_unix_ms",
    "updated_unix_ms",
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


def _worker_summary(entry: Mapping[str, Any]) -> dict[str, Any]:
    workers = entry.get("workers")
    rows = workers if isinstance(workers, list) else []
    planned = [row for row in rows if isinstance(row, Mapping) and row.get("provider")]
    return {
        "recorded": len(rows),
        "planned": len(planned),
        "launched": sum(
            1
            for row in planned
            if row.get("launch_state") in ("provider_reconciled", "verified")
        ),
        "branches": sorted(
            {
                str(row.get("branch"))
                for row in rows
                if isinstance(row, Mapping) and _text(row.get("branch"))
            }
        ),
    }


def _project_intakes(
    intakes: Iterable[Mapping[str, Any]], roots: Sequence[str]
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for entry in intakes:
        if not isinstance(entry, Mapping):
            continue
        matched = _belongs(_text(entry.get("source_cwd")), roots)
        if matched is None:
            continue
        row = {field: entry.get(field) for field in INTAKE_FIELDS if field in entry}
        row["project_root"] = matched
        row["workers"] = _worker_summary(entry)
        found.append(row)
        if len(found) >= MAX_INTAKES:
            break
    return found


def project_context(
    project: Project,
    *,
    sessions: Iterable[Mapping[str, Any]] = (),
    intakes: Iterable[Mapping[str, Any]] = (),
    group: Sequence[Project] | None = None,
    unavailable: Sequence[str] = (),
) -> dict[str, Any]:
    """Everything the kit knows about one project, in one record.

    ``unavailable`` names the stores the caller could not read, so a context
    with no intakes because the spool is missing never reads the same as a
    context with no intakes because there is no delegated work.
    """
    roots = _member_roots(project, group)
    found_sessions = _project_sessions(sessions, roots, project.group)
    found_intakes = _project_intakes(intakes, roots)
    manifest = project.manifest if isinstance(project.manifest, Mapping) else {}
    team = manifest.get("team", [])
    open_intakes = [
        row
        for row in found_intakes
        if row.get("state") in ("received", "acknowledged", "delegated")
    ]
    return {
        "project": project.to_dict(),
        "roots": roots,
        "sessions": found_sessions,
        "intakes": found_intakes,
        "team": [dict(role) for role in team] if isinstance(team, list) else [],
        "counts": {
            "sessions": len(found_sessions),
            "intakes": len(found_intakes),
            "open_intakes": len(open_intakes),
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
