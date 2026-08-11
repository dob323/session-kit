"""Turn a project into the exact plan ``sp new`` should launch.

``sp new <alias>`` used to resolve one thing: the alias to a directory and a
default provider. Everything else that makes a session the right session — the
account, the model, the command that sets it up, the workers a delegated run
needs — lived in the head of whoever typed the command, or in a chat message
that scrolled away.

A plan here is that missing half, assembled from the committed manifest and
the host's own shortcut row, with the precedence stated once:

    an explicit flag  >  a trusted manifest  >  the shortcut row  >  a default

Startup commands
----------------
``model`` and ``account`` select among things this host already has; a
manifest cannot conjure either. ``startup`` is different — it is a command
line, arriving from a repository, that would run on this machine. So a
startup command is approved once per project by its exact text, and the
approval is recorded locally by digest. Editing the command in the repository
withdraws the approval, and a non-interactive launch never approves anything:
a delegated worker starts without the startup command and says so, rather
than running a line nobody read.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Mapping

from .identity import Project, Resolver

PROVIDERS = ("claude", "codex", "shell")
DEFAULT_PROVIDER = "shell"
APPROVAL_FILE = "startup-approvals.tsv"
MAX_APPROVAL_BYTES = 256 * 1024

APPROVED = "approved"
UNAPPROVED = "unapproved"
CHANGED = "changed"


def startup_digest(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def _approval_path(state_dir: Path | str) -> Path:
    return Path(state_dir) / "projects" / APPROVAL_FILE


def _read_approvals(state_dir: Path | str | None) -> dict[str, str]:
    if state_dir is None:
        return {}
    path = _approval_path(state_dir)
    try:
        if path.is_symlink() or not path.is_file():
            return {}
        info = path.stat()
        if info.st_size > MAX_APPROVAL_BYTES:
            return {}
        if info.st_uid != os.getuid() or info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            # A record another account can write is not a record of what this
            # account approved.
            return {}
        text = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    approvals: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 2 or not fields[0].startswith("/"):
            continue
        approvals[fields[0]] = fields[1].strip()
    return approvals


def startup_state(
    root: str, command: str | None, state_dir: Path | str | None
) -> str | None:
    """``approved``, ``changed``, ``unapproved``, or ``None`` with no command."""
    if not command:
        return None
    recorded = _read_approvals(state_dir).get(root)
    if recorded is None:
        return UNAPPROVED
    return APPROVED if recorded == startup_digest(command) else CHANGED


def approve_startup(root: str, command: str, state_dir: Path | str) -> None:
    """Record that this account approved this exact startup command."""
    approvals = _read_approvals(state_dir)
    approvals[root] = startup_digest(command)
    path = _approval_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = "".join(
        f"{key}\t{value}\n" for key, value in sorted(approvals.items())
    ).encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _manifest_value(project: Project, key: str) -> Any:
    if not project.trusted or project.manifest is None:
        return None
    return project.manifest.get(key)


def launch_plan(
    project: Project,
    *,
    requested_provider: str | None = None,
    requested_account: str | None = None,
    requested_model: str | None = None,
    state_dir: Path | str | None = None,
    interactive: bool = False,
) -> dict[str, Any]:
    """The full launch plan for one project, with every choice explained.

    ``decisions`` records where each applied value came from, so ``sp new``
    and the picker can tell a person that the model was the manifest's and
    the account was theirs — instead of a session that quietly differs from
    what they typed.
    """
    decisions: dict[str, str] = {}
    notes: list[str] = list(project.warnings)

    provider = None
    if requested_provider:
        if requested_provider not in PROVIDERS:
            raise ValueError(f"unknown provider: {requested_provider}")
        provider, decisions["provider"] = requested_provider, "flag"
    if provider is None:
        value = _manifest_value(project, "provider")
        if value:
            provider, decisions["provider"] = str(value), "manifest"
    if provider is None and project.shortcut_provider in PROVIDERS:
        provider, decisions["provider"] = project.shortcut_provider, "shortcut"
    if provider is None:
        provider, decisions["provider"] = DEFAULT_PROVIDER, "default"

    account = None
    if requested_account:
        account, decisions["account"] = requested_account, "flag"
    else:
        value = _manifest_value(project, "account")
        if value:
            account, decisions["account"] = str(value), "manifest"

    model = None
    if requested_model:
        model, decisions["model"] = requested_model, "flag"
    else:
        value = _manifest_value(project, "model")
        if value:
            model, decisions["model"] = str(value), "manifest"

    if provider == "shell" and (account or model):
        # A shell session has no provider to give an account or a model to.
        if account:
            notes.append(
                "a shell session takes no account; the account was not applied"
            )
            account, decisions["account"] = None, "not-applicable"
        if model:
            notes.append("a shell session takes no model; the model was not applied")
            model, decisions["model"] = None, "not-applicable"

    startup = _manifest_value(project, "startup")
    startup_text = str(startup) if startup else None
    state = startup_state(project.root, startup_text, state_dir)
    startup_applied = False
    if startup_text:
        if state == APPROVED:
            startup_applied, decisions["startup"] = True, "manifest"
        elif not interactive:
            decisions["startup"] = "needs-approval"
            notes.append(
                "the project's startup command has not been approved on this host "
                "and this launch is not interactive, so it was not run"
            )
        else:
            decisions["startup"] = "needs-approval"

    team = []
    manifest = project.manifest if project.manifest is not None else {}
    for role in manifest.get("team", []) if isinstance(manifest, Mapping) else []:
        team.append(dict(role))
    if team and not project.trusted:
        notes.append(
            "team roles are listed from the manifest but will not be launched "
            "until the project is on the host's project list"
        )

    return {
        "root": project.root,
        "name": project.name,
        "alias": project.alias,
        "group_root": project.group,
        "is_worktree": project.is_worktree,
        "trusted": project.trusted,
        "source": project.source,
        "provider": provider,
        "account": account,
        "model": model,
        "startup": startup_text,
        "startup_state": state,
        "startup_applied": startup_applied,
        "startup_digest": startup_digest(startup_text) if startup_text else None,
        "team": team,
        "team_launchable": bool(team) and project.trusted,
        "decisions": decisions,
        "manifest_path": project.manifest_path,
        "manifest_error": project.manifest_error,
        "notes": notes,
    }


def plan_for_target(
    resolver: Resolver,
    target: str,
    **kwargs: Any,
) -> dict[str, Any] | None:
    """Plan for an alias, or for the project containing a directory."""
    project = resolver.resolve_alias(target)
    if project is None and (
        target.startswith("/") or target.startswith(".") or os.sep in target
    ):
        project = resolver.resolve(target)
    if project is None:
        return None
    return launch_plan(project, **kwargs)
