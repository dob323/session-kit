"""Choose the short Claude project-directory name for one launch.

Claude Code 2.1.234 added ``CLAUDE_CODE_PROJECT_DIR_NAME``: when
``CLAUDE_CONFIG_DIR`` is set, the variable names the per-project directory
under ``<config>/projects/`` instead of the munged working-directory path.
This helper decides whether one launch may use it, and prints the name on
stdout when — and only when — every proof holds:

* the installed Claude launcher resolves to a version that knows the
  variable (2.1.234 or newer); an older Claude ignores an exported name
  silently, which would strand new transcripts under the munged name the
  moment the export started working after an upgrade;
* the launch directory is exactly the registered root of a project shortcut,
  and exactly one shortcut claims that root;
* the alias satisfies Claude's own validator (``[A-Za-z0-9_-]{1,64}``, not a
  Windows reserved device name) — Claude falls back *silently* on a bad
  value, so the refusal has to happen here where it can be reasoned about;
* the profile holds no legacy munged directory for this root, or that
  directory was just renamed to the alias. The rename is refused while any
  process still carries this profile with a working directory inside the
  root, because Claude re-resolves the path per write: renaming under a live
  session splits its transcript, and auto memory is only ever read from the
  resolved name — a half-migrated project is a session with amnesia.

Anything unprovable prints nothing and exits 0: the launch simply keeps
today's munged-name behaviour. This file is invoked by path from the session
shell (like ``session_inventory.py``) and therefore imports nothing from the
package.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import sys


MINIMUM_CLAUDE_VERSION = (2, 1, 234)
CLAUDE_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
RESERVED_NAME_RE = re.compile(r"(?:con|prn|aux|nul|com[0-9]|lpt[0-9])", re.IGNORECASE)
LAUNCHER_VERSION_RE = re.compile(r"/versions/([0-9]+)\.([0-9]+)\.([0-9]+)$")
MUNGE_RE = re.compile(r"[^A-Za-z0-9]")


def claude_supports_project_dir_name(command: str = "claude") -> bool:
    """True only when the resolved launcher proves a new-enough version.

    The proof is the immutable-release layout the installer creates
    (``.../versions/<major>.<minor>.<patch>``). A launcher that resolves
    anywhere else proves nothing and the answer is no — never a guess via
    ``claude --version``, which costs a process start on every launch.
    """
    located = shutil.which(command)
    if located is None:
        return False
    try:
        resolved = os.path.realpath(located)
    except OSError:
        return False
    match = LAUNCHER_VERSION_RE.search(resolved)
    if match is None:
        return False
    version = tuple(int(part) for part in match.groups())
    return version >= MINIMUM_CLAUDE_VERSION


def alias_for_root(projects_file: Path, cwd: Path) -> str | None:
    """The single registered alias whose root is exactly ``cwd``.

    Rows follow ``projects.tsv``: ``alias<TAB>provider<TAB>root``. Two rows
    claiming the same root is ambiguity, and ambiguity is a refusal, not a
    pick.
    """
    try:
        payload = projects_file.read_text(encoding="utf-8")
        resolved_cwd = cwd.resolve(strict=True)
    except (OSError, ValueError):
        return None
    matches: list[str] = []
    for line in payload.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 3:
            continue
        alias, _provider, root = fields[0], fields[1], fields[2]
        try:
            if Path(root).resolve(strict=True) != resolved_cwd:
                continue
        except OSError:
            continue
        matches.append(alias)
    if len(matches) != 1:
        return None
    alias = matches[0]
    if not CLAUDE_NAME_RE.fullmatch(alias) or RESERVED_NAME_RE.fullmatch(alias):
        return None
    return alias


# The launch chain a migration may excuse: shells, multiplexers, and session
# plumbing. A provider process is deliberately NOT here — a Claude ancestor
# that carries this profile inside this root is a live session whatever its
# position in the tree, and renaming under it splits its store. Anything
# unrecognised counts as live; refusal is the safe direction.
EXCUSABLE_ANCESTOR_COMMS = frozenset(
    {
        "bash",
        "zsh",
        "fish",
        "sh",
        "dash",
        "script",
        "shpool",
        "sshd",
        "systemd",
        "login",
        "tmux",
        "screen",
        "python3",
        "python",
    }
)


def _ancestor_pids() -> set[int]:
    pids: set[int] = set()
    pid = os.getpid()
    for _ in range(64):
        pids.add(pid)
        try:
            stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            pid = int(stat_line.rsplit(") ", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
        if pid <= 1:
            pids.add(pid)
            break
    return pids


def profile_live_in_root(profile: Path, root: Path, proc: Path = Path("/proc")) -> bool:
    """True when any *other* process runs this profile inside this root.

    A live session is any process whose environment carries this exact
    ``CLAUDE_CONFIG_DIR`` and whose working directory sits at or under the
    project root. The launching shell and this helper both match that
    description themselves — their whole ancestor chain is excluded, or the
    migration would wait on the very launch that requested it. Unreadable
    process entries count as live: an unproven quiet is not quiet.
    """
    needle = b"CLAUDE_CONFIG_DIR=" + os.fsencode(os.fspath(profile)) + b"\0"
    root_text = os.fspath(root)
    own = _ancestor_pids()
    try:
        entries = list(proc.iterdir())
    except OSError:
        return True
    for entry in entries:
        if not entry.name.isdigit():
            continue
        excused_ancestor = int(entry.name) in own
        try:
            environ = (entry / "environ").read_bytes()
        except OSError:
            continue
        if needle not in environ:
            continue
        if excused_ancestor:
            # The chain that requested this launch matches its own scan by
            # construction — but only its shells and plumbing are excused. A
            # provider process above us is a session inside a session, and a
            # rename under it splits ITS store (proven by review lane
            # rv-pdn-1, 2026-08-17). An unreadable comm counts as live.
            try:
                comm = (entry / "comm").read_text(encoding="ascii").strip()
            except OSError:
                return True
            if comm in EXCUSABLE_ANCESTOR_COMMS:
                continue
        try:
            cwd = os.readlink(entry / "cwd")
        except OSError:
            return True
        if cwd == root_text or cwd.startswith(root_text + os.sep):
            return True
    return False


def resolve_project_dir_name(
    profile: Path, cwd: Path, projects_file: Path, claude_command: str = "claude"
) -> str | None:
    if os.environ.get("SESSION_KIT_PROJECT_DIR_NAME") == "off":
        return None
    if not claude_supports_project_dir_name(claude_command):
        return None
    alias = alias_for_root(projects_file, cwd)
    if alias is None:
        return None
    try:
        root = cwd.resolve(strict=True)
    except OSError:
        return None
    munged = MUNGE_RE.sub("-", os.fspath(root))
    projects_dir = profile / "projects"
    legacy = projects_dir / munged
    renamed = projects_dir / alias
    # The alias path must be a real directory or absent. Claude cannot use a
    # regular file or a symlink as its project directory, and exporting the
    # name anyway would strand the launch's writes (review lane rv-pdn-1).
    if renamed.exists() and (renamed.is_symlink() or not renamed.is_dir()):
        return None
    if not legacy.is_dir() or legacy.is_symlink():
        return alias
    # Two windows of the same project can launch in the same second, and both
    # would pass the liveness scan (neither is running yet). Unserialised,
    # the rename loser would print nothing and its Claude would recreate the
    # munged directory beside the renamed one — the exact split state the
    # both-names refusal below exists to prevent. One advisory lock makes the
    # decision serial: the loser re-reads the world the winner left behind.
    try:
        projects_dir.mkdir(mode=0o700, exist_ok=True)
        lock = os.open(
            projects_dir / ".project-dir-name.lock",
            os.O_CREAT | os.O_RDWR,
            0o600,
        )
    except OSError:
        return None
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not legacy.is_dir() or legacy.is_symlink():
            return alias
        if renamed.exists():
            # Both directories exist: someone already holds state under each
            # name. Renaming would clobber and exporting would split memory
            # between them — this needs a person, not a launch-time guess.
            return None
        if profile_live_in_root(profile, root):
            return None
        try:
            legacy.rename(renamed)
        except OSError:
            return None
        # A raw provider launch takes no part in this lock, so one can start
        # in the gap between the scan above and the rename. Scan again on the
        # far side: if a same-profile process now runs inside the root, its
        # first write would recreate the munged path beside the moved one —
        # the split-store defect review lane rv-pdn-1 reproduced. Undo the
        # move and refuse; the raw session then finds the world exactly as it
        # was. An undo that fails leaves the both-names state the next launch
        # refuses on and doctor reports — visible, never silent.
        if profile_live_in_root(profile, root):
            try:
                renamed.rename(legacy)
            except OSError:
                pass
            return None
        return alias
    finally:
        os.close(lock)


def doctor_report(projects_file: Path, accounts_root: Path) -> tuple[str, str]:
    """One ``(status, detail)`` line for ``session-kit doctor``.

    ``warn`` names the states a person has to resolve — a registry root
    claimed twice, a profile holding state under both the munged and the
    short name, or a profile settings file without the auto-continue switch.
    Directories still waiting on their automatic launch-time rename are
    ordinary and reported inside ``ok``.
    """
    problems: list[str] = []
    pending = 0
    rows: list[tuple[str, Path]] = []
    try:
        payload = projects_file.read_text(encoding="utf-8")
    except OSError:
        payload = ""
    seen_roots: dict[Path, str] = {}
    for line in payload.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 3:
            continue
        alias, root_raw = fields[0], fields[2]
        try:
            root = Path(root_raw).resolve(strict=True)
        except OSError:
            continue
        if root in seen_roots and seen_roots[root] != alias:
            problems.append(
                f"{root} is registered as both {seen_roots[root]} and {alias}"
            )
            continue
        seen_roots[root] = alias
        if CLAUDE_NAME_RE.fullmatch(alias) and not RESERVED_NAME_RE.fullmatch(alias):
            rows.append((alias, root))
    try:
        profiles = sorted(entry for entry in accounts_root.iterdir() if entry.is_dir())
    except OSError:
        profiles = []
    for profile in profiles:
        settings = profile / "settings.json"
        try:
            value = json.loads(settings.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or "autoContinueAtUsageLimit" not in value:
                problems.append(
                    f"{profile.name} settings lack autoContinueAtUsageLimit"
                )
        except (OSError, ValueError):
            problems.append(f"{profile.name} settings are unreadable")
        for alias, root in rows:
            munged = profile / "projects" / MUNGE_RE.sub("-", os.fspath(root))
            renamed = profile / "projects" / alias
            if munged.is_dir() and renamed.exists():
                problems.append(f"{profile.name} holds {alias} under both names")
            elif munged.is_dir():
                pending += 1
    if problems:
        return "warn", "; ".join(problems)[:500]
    detail = f"{len(rows)} named projects across {len(profiles)} profiles"
    if pending:
        detail += f"; {pending} awaiting their automatic rename at next quiet launch"
    return "ok", detail


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile")
    parser.add_argument("--cwd")
    parser.add_argument(
        "--projects-file",
        # The same registry every other consumer reads: the explicit override
        # first, then the XDG location. A custom registry that named one root
        # while this default read another exported the wrong short name
        # (review lane rv-pdn-2, 2026-08-17).
        default=os.environ.get("SESSION_KIT_PROJECTS_FILE")
        or os.path.join(
            os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
            "session-kit",
            "projects.tsv",
        ),
    )
    parser.add_argument("--claude-command", default="claude")
    parser.add_argument("--doctor", action="store_true")
    parser.add_argument("--accounts-root")
    options = parser.parse_args(argv)
    if options.doctor:
        account_root = os.environ.get("SESSION_KIT_ACCOUNT_ROOT")
        default_root = (
            Path(account_root) / "claude"
            if account_root
            else Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
            / "session-kit"
            / "accounts"
            / "claude"
        )
        status, detail = doctor_report(
            Path(options.projects_file),
            Path(options.accounts_root) if options.accounts_root else default_root,
        )
        print(f"{status}\t{detail}")
        return 0
    if not options.profile or not options.cwd:
        parser.error("--profile and --cwd are required outside --doctor")
    name = resolve_project_dir_name(
        Path(options.profile),
        Path(options.cwd),
        Path(options.projects_file),
        options.claude_command,
    )
    if name is not None:
        print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
