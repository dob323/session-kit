"""The machine surface the shell and the picker call.

Every verb prints one JSON object on standard output and returns 0, or prints
a message on standard error and returns non-zero. Nothing here writes to the
terminal a person is looking at: the callers own presentation, this owns the
answer.

Exit codes
    0  the answer is on standard output
    1  the target does not resolve to a project
    2  the arguments are wrong
    3  a manifest is malformed
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

if __package__ in (None, ""):  # the shell runs this file as a script
    sys.path.insert(0, os.fspath(Path(__file__).resolve().parents[1]))
    from sessionkit_projects import context as context_module  # noqa: E402
    from sessionkit_projects import identity, launch, manifest  # noqa: E402
else:
    from . import context as context_module
    from . import identity, launch, manifest

EXIT_OK = 0
EXIT_NO_PROJECT = 1
EXIT_USAGE = 2
EXIT_MANIFEST = 3

MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024


def _print(value: object) -> None:
    json.dump(value, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _state_dir(args: argparse.Namespace, environ: Mapping[str, str]) -> Path:
    if getattr(args, "state_dir", None):
        return Path(args.state_dir)
    explicit = environ.get("SESSION_KIT_STATE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    base = environ.get("XDG_STATE_HOME") or os.path.join(
        environ.get("HOME", str(Path.home())), ".local", "state"
    )
    return Path(base) / "session-kit"


def _resolver(
    args: argparse.Namespace, environ: Mapping[str, str]
) -> identity.Resolver:
    return identity.Resolver(getattr(args, "projects_file", None), environ=environ)


def _target(resolver: identity.Resolver, target: str | None) -> identity.Project | None:
    """An alias, a directory, or — with neither — the current directory."""
    if not target:
        return resolver.resolve(os.getcwd())
    project = resolver.resolve_alias(target)
    if project is not None:
        return project
    if target.startswith(("/", ".", "~")) or os.sep in target:
        return resolver.resolve(target)
    return None


# ---- reading the other stores -------------------------------------------


def _read_snapshot(path: str | None) -> tuple[list[dict[str, Any]], list[str]]:
    """Session rows from a snapshot file, or from the live inventory.

    A context that cannot read the inventory says so; it never reports an
    empty session list as though the project were idle.
    """
    if path:
        try:
            source = Path(path)
            if source.stat().st_size > MAX_SNAPSHOT_BYTES:
                return [], ["the session snapshot is too large to read"]
            payload = source.read_text(encoding="utf-8")
        except (OSError, ValueError) as error:
            return [], [f"the session snapshot is unreadable: {error}"]
    else:
        import subprocess  # noqa: PLC0415 — only the live path pays for this

        tool = Path(__file__).resolve().parents[1] / "session_inventory.py"
        try:
            completed = subprocess.run(
                [sys.executable, os.fspath(tool), "snapshot", "--no-write"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return [], [f"the session inventory is unavailable: {error}"]
        if completed.returncode != 0:
            return [], ["the session inventory is unavailable"]
        payload = completed.stdout
    try:
        value = json.loads(payload)
    except ValueError:
        return [], ["the session inventory returned unreadable data"]
    rows = value.get("sessions") if isinstance(value, Mapping) else None
    if not isinstance(rows, list):
        return [], ["the session inventory returned no session list"]
    return [row for row in rows if isinstance(row, dict)], []


# ---- verbs --------------------------------------------------------------


def _resolve(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    resolver = _resolver(args, environ)
    project = _target(resolver, args.target)
    if project is None:
        print("no project covers that directory", file=sys.stderr)
        return EXIT_NO_PROJECT
    _print({"project": project.to_dict(), "warnings": list(resolver.warnings)})
    return EXIT_OK


def _list(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    resolver = _resolver(args, environ)
    projects = resolver.projects()
    _print(
        {
            "projects": [project.to_dict() for project in projects],
            "groups": identity.group_projects(projects),
            "warnings": list(resolver.warnings),
        }
    )
    return EXIT_OK


def _launch_plan(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    resolver = _resolver(args, environ)
    project = _target(resolver, args.target)
    if project is None:
        print(f"unknown or invalid project: {args.target}", file=sys.stderr)
        return EXIT_NO_PROJECT
    try:
        plan = launch.launch_plan(
            project,
            requested_provider=args.provider,
            requested_account=args.account,
            requested_model=args.model,
            state_dir=_state_dir(args, environ),
            interactive=args.interactive,
        )
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return EXIT_USAGE
    if args.format == "tsv":
        return _print_plan_tsv(plan)
    _print(plan)
    return EXIT_OK


# The fields `sp new` needs, in one line the shell can read with `read -r`.
# A shell caller cannot recover from a value containing a tab or a newline, so
# rather than emit an ambiguous line this refuses and the caller keeps the
# JSON path.
PLAN_TSV_FIELDS = ("root", "provider", "account", "model", "startup_state")


def _print_plan_tsv(plan: Mapping[str, Any]) -> int:
    fields = []
    for name in PLAN_TSV_FIELDS:
        value = plan.get(name)
        text = "" if value is None else str(value)
        if "\t" in text or "\n" in text or "\r" in text:
            print(
                f"the project's {name} contains a tab or newline and cannot be "
                "passed to the shell",
                file=sys.stderr,
            )
            return EXIT_USAGE
        fields.append(text)
    sys.stdout.write("\t".join(fields) + "\n")
    return EXIT_OK


def _approve_startup(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    resolver = _resolver(args, environ)
    project = _target(resolver, args.target)
    if project is None:
        print(f"unknown or invalid project: {args.target}", file=sys.stderr)
        return EXIT_NO_PROJECT
    values = project.manifest or {}
    command = values.get("startup") if isinstance(values, Mapping) else None
    if not command:
        print("that project's manifest declares no startup command", file=sys.stderr)
        return EXIT_USAGE
    if not project.trusted:
        print(
            "add the project with `session-kit projects add` before approving "
            "its startup command",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if args.expect and args.expect != launch.startup_digest(str(command)):
        # The command the operator was shown must be the command approved.
        print("the startup command changed since it was shown", file=sys.stderr)
        return EXIT_USAGE
    launch.approve_startup(project.root, str(command), _state_dir(args, environ))
    _print(
        {
            "root": project.root,
            "startup": command,
            "startup_digest": launch.startup_digest(str(command)),
            "approved": True,
        }
    )
    return EXIT_OK


def _context(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    resolver = _resolver(args, environ)
    project = _target(resolver, args.target)
    if project is None:
        print("no project covers that directory", file=sys.stderr)
        return EXIT_NO_PROJECT
    sessions, session_notes = _read_snapshot(args.snapshot)
    _print(
        context_module.project_context(
            project,
            sessions=sessions,
            group=resolver.projects(),
            unavailable=list(session_notes),
        )
    )
    return EXIT_OK


def _group_sessions(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    resolver = _resolver(args, environ)
    sessions, notes = _read_snapshot(args.snapshot)
    assignments = resolver.assign(
        str(row.get("cwd")) for row in sessions if isinstance(row.get("cwd"), str)
    )
    grouped = context_module.group_sessions_by_project(
        sessions, assignments, projects=resolver.projects()
    )
    grouped["unavailable"] = notes
    grouped["warnings"] = list(resolver.warnings)
    _print(grouped)
    return EXIT_OK


def _check(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    path = Path(args.path)
    if path.is_dir():
        path = path / manifest.MANIFEST_NAME
    try:
        values = manifest.read(path)
    except manifest.ManifestError as error:
        print(str(error), file=sys.stderr)
        return EXIT_MANIFEST
    _print({"path": os.fspath(path), "manifest": values, "valid": True})
    return EXIT_OK


# ---- entry point --------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="session-kit projects",
        description="Resolve projects, their manifests, and their context.",
    )
    parser.add_argument("--projects-file", help="path to projects.tsv")
    parser.add_argument("--state-dir", help="path to the Session Kit state directory")
    subparsers = parser.add_subparsers(dest="command", required=True)

    resolve = subparsers.add_parser(
        "resolve", help="the project a directory belongs to"
    )
    resolve.add_argument(
        "target", nargs="?", help="alias or directory; default is $PWD"
    )
    resolve.set_defaults(handler=_resolve)

    listing = subparsers.add_parser("list", help="every project this host knows")
    listing.set_defaults(handler=_list)

    plan = subparsers.add_parser("launch-plan", help="what `sp new` should start")
    plan.add_argument("target", nargs="?")
    plan.add_argument("--provider", choices=launch.PROVIDERS)
    plan.add_argument("--account")
    plan.add_argument("--model")
    plan.add_argument(
        "--interactive",
        action="store_true",
        help="a person can answer, so an unapproved startup command may be offered",
    )
    plan.add_argument(
        "--format",
        choices=("json", "tsv"),
        default="json",
        help="tsv prints one line: " + ", ".join(PLAN_TSV_FIELDS),
    )
    plan.set_defaults(handler=_launch_plan)

    approve = subparsers.add_parser(
        "approve-startup", help="record approval of a project's startup command"
    )
    approve.add_argument("target", nargs="?")
    approve.add_argument("--expect", help="digest of the command that was shown")
    approve.set_defaults(handler=_approve_startup)

    context = subparsers.add_parser("context", help="what is going on in a project")
    context.add_argument("target", nargs="?")
    context.add_argument("--snapshot", help="read sessions from this snapshot file")
    context.set_defaults(handler=_context)

    grouped = subparsers.add_parser(
        "group-sessions", help="bucket the session list by project"
    )
    grouped.add_argument("--snapshot", help="read sessions from this snapshot file")
    grouped.set_defaults(handler=_group_sessions)

    check = subparsers.add_parser("check", help="validate a session-kit.toml")
    check.add_argument("path", help="the manifest, or the directory holding it")
    check.set_defaults(handler=_check)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    handler = args.handler
    try:
        return int(handler(args, os.environ))
    except manifest.ManifestError as error:
        print(str(error), file=sys.stderr)
        return EXIT_MANIFEST
    except BrokenPipeError:
        return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
