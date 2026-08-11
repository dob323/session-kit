"""Projects as one first-class thing.

A project is a canonical absolute root directory. A ``projects.tsv`` shortcut
names one, a committed ``session-kit.toml`` describes one, an intake arrives
from inside one, and a session runs in one. :mod:`.identity` holds the single
membership rule the rest of the kit resolves against; :mod:`.manifest` reads
the committed description; :mod:`.launch` turns a project into what ``sp new``
should start; :mod:`.context` answers what is already going on there.
"""

from __future__ import annotations

from .context import group_sessions_by_project, project_context
from .identity import (
    MANIFEST_NAME,
    Project,
    Resolver,
    canonical,
    default_projects_file,
    group_projects,
    main_repository,
)
from .launch import approve_startup, launch_plan, plan_for_target, startup_state
from .manifest import ManifestError, loads as load_manifest, read as read_manifest

__all__ = [
    "MANIFEST_NAME",
    "ManifestError",
    "Project",
    "Resolver",
    "approve_startup",
    "canonical",
    "default_projects_file",
    "group_projects",
    "group_sessions_by_project",
    "launch_plan",
    "load_manifest",
    "main_repository",
    "plan_for_target",
    "project_context",
    "read_manifest",
    "startup_state",
]
