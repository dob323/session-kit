#!/usr/bin/env bash
# Session Kit install, update, and rollback commands, including the release
# payload and platform service units. Source this file; do not execute it.
#
# Source order: bin/session-kit sources this module after it assigns the
# lifecycle roots, shpool_config, helpers, systemd_units, launchd_units,
# launchd_template_root, receipt_path, integration_marker, codex_theme_names,
# and projects_created, and after it defines die(), platform(), and
# require_arg(). These commands call into lib/sh/session_kit_checks.sh,
# lib/sh/session_kit_lifecycle.sh, lib/sh/session_kit_login.sh, and
# lib/sh/session_kit_projects.sh, all of which only have to be sourced before
# the dispatcher runs.
#
# Globals the entry script owns are assigned there, not here.
# shellcheck disable=SC2154

# Pin the transaction's launcher source before any current-release pointer can
# move. A direct management entry from an older installation may source this
# module through $install_root/current; resolving BASH_SOURCE later, after a
# rollback flip, would silently select the rollback target's obsolete launcher.
session_kit_launcher_source=$(
  cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd
)/deploy/session-kit-launcher

install_launchers() {
  local release_id=$1 helper source destination temporary
  # Launchers are recovery-capable and release-independent. Always install the
  # copy belonging to the code performing this transaction, including for a
  # rollback to a release whose older manager cannot read today's journal.
  source=$session_kit_launcher_source
  [[ -f $source && ! -L $source ]] || die "stable Session Kit launcher is unavailable"
  mkdir -p "$bin_dir"
  for helper in "${helpers[@]}"; do
    destination=$bin_dir/$helper
    temporary=$(mktemp "$bin_dir/.${helper}.XXXXXX")
    if ! install -m 0755 "$source" "$temporary" ||
       ! mv -f -- "$temporary" "$destination"; then
      rm -f -- "$temporary"
      return 1
    fi
  done
  destination=$bin_dir/session-kit
  temporary=$(mktemp "$bin_dir/.session-kit.XXXXXX")
  if ! install -m 0755 "$source" "$temporary" ||
     ! mv -f -- "$temporary" "$destination"; then
    rm -f -- "$temporary"
    return 1
  fi
}

# The shipped Bash completion, copied once per command name into the directory
# bash-completion loads user files from. Copies rather than symlinks: the file
# lives in a read-only release that a rollback replaces, and a dangling symlink
# in a completion directory makes every new shell print an error.
#
# It is never fatal. A machine without the target directory writable, or with a
# name already taken by something that is not ours, keeps its completion as it
# was and the rest of the installation proceeds -- completion is a convenience,
# and refusing an install over it would be the wrong trade.
install_completions() {
  local release_id=$1 source name destination temporary
  [[ $completion_dir != none ]] || return 0
  source=$install_root/releases/$release_id/lib/sh/sp_completion.bash
  [[ -f $source && ! -L $source ]] || return 0
  [[ ! -e $completion_dir || ( -d $completion_dir && ! -L $completion_dir ) ]] ||
    return 0
  mkdir -p -- "$completion_dir" 2>/dev/null || return 0
  for name in "${completion_names[@]}"; do
    destination=$completion_dir/$name
    # Anything already there that is not a kit copy belongs to the user or to
    # a package: leave it exactly as it is.
    if [[ -e $destination || -L $destination ]]; then
      [[ -f $destination && ! -L $destination ]] || continue
      grep -qxF -- "$completion_marker" "$destination" 2>/dev/null || continue
    fi
    temporary=$(mktemp "$completion_dir/.${name}.XXXXXX" 2>/dev/null) || continue
    if install -m 0644 "$source" "$temporary" 2>/dev/null &&
       mv -f -- "$temporary" "$destination" 2>/dev/null; then
      continue
    fi
    rm -f -- "$temporary"
  done
  return 0
}

install_codex_themes() {
  local release_id=$1 codex_theme_root theme_name theme_file destination temporary
  if [[ ! -d $install_root/releases/$release_id/config/codex-themes ]]; then
    local -a obsolete_themes=()
    mapfile -t obsolete_themes < <(codex_theme_layout targets)
    for destination in "${obsolete_themes[@]}"; do
      [[ ! -d $destination || -L $destination ]] ||
        die "refusing to replace theme directory: $destination"
      rm -f -- "$destination"
    done
    return 0
  fi
  codex_theme_root=$(codex_theme_layout create)
  # Install the themes THIS release ships, rather than the names this program
  # happens to know. Those are not the same list, in both directions. An update
  # runs under the PREVIOUS launcher, so a release that adds a colour would
  # install the older, smaller set and leave the new windows untinted until a
  # second update. A rollback has the mirror problem: the release being
  # restored predates names the running code knows, and requiring them refuses
  # the rollback outright.
  for theme_file in \
    "$install_root/releases/$release_id/config/codex-themes"/sk-*.tmTheme; do
    [[ -e $theme_file || -L $theme_file ]] || continue
    theme_name=${theme_file##*/}
    # The release directory is trusted only as far as its shape: install
    # nothing whose name is not exactly a kit theme, and nothing that is not a
    # plain file. Absence means a smaller palette; a symlink means a tampered
    # release.
    [[ $theme_name =~ ^sk-[a-z]+\.tmTheme$ ]] ||
      die "release Codex theme name is unsafe: $theme_name"
    [[ -f $theme_file && ! -L $theme_file ]] ||
      die "release Codex theme is unsafe: $theme_name"
    destination=$codex_theme_root/${theme_file##*/}
    temporary=$(mktemp "$codex_theme_root/.${theme_file##*/}.XXXXXX")
    if ! install -m 0600 "$theme_file" "$temporary"; then
      rm -f -- "$temporary"
      return 1
    fi
    if lifecycle_failpoint_armed theme-copy; then
      rm -f -- "$temporary"
      die "isolated test failpoint during theme copy"
    fi
    if ! mv -f -- "$temporary" "$destination"; then
      rm -f -- "$temporary"
      return 1
    fi
  done
}

# The kit-owned Codex tab title (K3), deployed as a template the launcher
# reads -- never as an edit to the person's own ~/.codex/config.toml. Codex
# takes the same key on the command line and a command-line value wins for that
# process, so a kit session gets the kit's title items and every other Codex
# window keeps whatever the personal config says. Removing the template returns
# kit sessions to the built-in default; SESSION_KIT_TAB_TITLE=off turns the
# whole feature off on both providers.
#
# Same safety rule as the /kit verb above: an unowned, world-writable, or
# non-plain destination is left alone rather than replaced, and a machine with
# no Codex is not an error.
install_codex_terminal_title() {
  local release_id=$1 source_file
  source_file=$install_root/releases/$release_id/config/codex/terminal-title.toml
  [[ -f $source_file && ! -L $source_file ]] || return 0
  # A refusal (no Codex, an unsafe destination) exits 0 and leaves everything
  # alone; a FAILED WRITE exits nonzero and takes the install with it, because
  # a template that silently did not land is a Codex tab that silently keeps
  # the vendor's title.
  python3 - "$source_file" \
    "${SESSION_KIT_CODEX_HOME:-${CODEX_HOME:-$HOME/.codex}}" <<'KITTITLE' || return 1
import os
import pathlib
import stat
import sys
import tempfile

source_raw, codex_raw = sys.argv[1:3]
root = pathlib.Path(codex_raw)
if not root.is_absolute() or ".." in root.parts:
    raise SystemExit(0)


def owner_directory(path, *, create):
    """An owner-controlled real directory, or None. Never follows a symlink.

    Group-writable counts as writable by somebody else: this refuses 0o022,
    not just other-writable, since the file it guards is read at every Codex
    launch and turned into a command-line argument.
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        if not create:
            return None
        try:
            path.mkdir(mode=0o700)
        except OSError:
            return None
        return path
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        return None
    return path


# A Codex home that does not exist is a provider that is not installed here.
if owner_directory(root, create=False) is None:
    raise SystemExit(0)
directory = owner_directory(root / "session-kit", create=True)
if directory is None:
    raise SystemExit(0)
destination = directory / "terminal-title.toml"
try:
    existing = destination.lstat()
except FileNotFoundError:
    pass
else:
    # The stated rule, now enforced: a destination that is not a plain file
    # this account owns, or that anyone else can write, is left exactly as it
    # is. Type alone was checked before, so an unowned or group/world-writable
    # regular file was overwritten.
    if (
        stat.S_ISLNK(existing.st_mode)
        or not stat.S_ISREG(existing.st_mode)
        or existing.st_uid != os.geteuid()
        or stat.S_IMODE(existing.st_mode) & 0o022
    ):
        raise SystemExit(0)
temporary = None
try:
    body = pathlib.Path(source_raw).read_bytes()
    handle, temporary = tempfile.mkstemp(prefix=".terminal-title.", dir=directory)
    with os.fdopen(handle, "wb") as stream:
        stream.write(body)
    os.chmod(temporary, 0o600)
    os.replace(temporary, destination)
except OSError as exc:
    if temporary is not None:
        try:
            os.unlink(temporary)
        except OSError:
            pass
    print(
        f"session-kit: the Codex tab-title template could not be written: {exc}",
        file=sys.stderr,
    )
    raise SystemExit(1)
KITTITLE
  return 0
}

# `/kit` inside a live provider means "leave this running and give me the
# picker back". It is one line of shell -- `shpool detach`, which uses
# $SHPOOL_SESSION_NAME when no name is given -- but it has to be reachable
# from inside the conversation, so the kit ships it as a provider command and
# installs it beside the provider's own.
#
# Nothing here is best-effort about safety: a destination that is not an
# owner-controlled plain directory, or a file that is not a plain file, is
# left alone rather than replaced. A provider that is not installed on this
# machine simply has nowhere to put its copy, which is not an error.
#
# ONLY THE TYPED `/kit` MAY DETACH. Claude Code merged custom commands into
# skills, so a file in `commands/` is registered as a skill and, by default,
# the model may invoke it whenever the conversation looks relevant. The body
# opens with an injected `!`shpool detach``, which Claude Code runs when the
# skill content is RENDERED -- before the model reads a word of it -- and
# `allowed-tools` pre-approves it, so no prompt can catch it. An operator
# typed the bare word `kit` as an ordinary message, the model matched it
# against the description, and their terminal detached mid-turn while output was
# still painting (2026-08-15). `disable-model-invocation: true` in the
# template closes it at the first link: with it set, the description is not
# placed in the model's context at all, so there is nothing left to match,
# while the typed `/kit` keeps working exactly as before. The kit also teaches
# the bare word `kit` for the SHELL function, which is why that collision was
# always going to happen here and nowhere else.
install_provider_commands() {
  local release_id=$1 source_root
  source_root=$install_root/releases/$release_id/config/provider-commands
  [[ -d $source_root ]] || return 0
  # CLAUDE_CONFIG_DIR is deliberately NOT consulted. An install run from
  # inside a managed session inherits the account profile that session is
  # using, and installing a machine-wide verb into one rotating profile
  # directory puts it where the next account switch cannot see it. The kit's
  # other Claude-side install targets use $HOME/.claude for the same reason.
  python3 - "$source_root" "$HOME" \
    "${SESSION_KIT_CLAUDE_HOME:-$HOME/.claude}" \
    "${SESSION_KIT_CODEX_HOME:-${CODEX_HOME:-$HOME/.codex}}" \
    "${XDG_DATA_HOME:-$HOME/.local/share}/session-kit/accounts" <<'KITCMD' || return 1
import os
import pathlib
import stat
import sys
import tempfile

source_root, home_raw, claude_raw, codex_raw, accounts_raw = sys.argv[1:6]


def owner_directory(path, *, create):
    """An owner-controlled real directory, or None. Never follows a symlink."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        if not create:
            return None
        try:
            path.mkdir(mode=0o700)
        except OSError:
            return None
        return path
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o002
    ):
        return None
    return path


def install_one(root_raw, subdirectory, name):
    root = pathlib.Path(root_raw)
    if not root.is_absolute() or ".." in root.parts:
        return
    # A provider home that does not exist is a provider that is not installed
    # on this machine, which is not a failure to report.
    if owner_directory(root, create=False) is None:
        return
    directory = owner_directory(root / subdirectory, create=True)
    if directory is None:
        return
    body = pathlib.Path(source_root, name).read_bytes()
    destination = directory / "kit.md"
    try:
        existing = destination.lstat()
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
            return
    handle, temporary = tempfile.mkstemp(prefix=".kit.md.", dir=directory)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(body)
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    except OSError:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


# Claude only. Codex got a copy at ~/.codex/prompts/kit.md until 2026-08-15,
# when it was measured to do nothing: in codex-cli 0.145.0 the slash namespace
# is built-in commands only, its app-server protocol carries no channel for a
# custom command, and ~/.codex/prompts/ is not read at all. Installing a file
# that a provider never reads is worse than installing none -- it is a claim
# the docs then repeat. Ctrl-q is the Codex answer and needs no file.
install_one(claude_raw, "commands", "claude-kit.md")

# An enrolled account is a WHOLE provider config directory, and a session the
# kit launches on that account reads its commands from there -- CLAUDE_CONFIG_DIR
# replaces the config root rather than adding to it. A verb installed only in
# ~/.claude is therefore invisible in exactly the sessions it exists for, which
# is what the live drill found. Every enrolled profile gets its own copy.
accounts = pathlib.Path(accounts_raw)
for provider, subdirectory, name in (("claude", "commands", "claude-kit.md"),):
    root = accounts / provider
    try:
        aliases = sorted(entry.name for entry in os.scandir(root))
    except OSError:
        continue
    for alias in aliases:
        install_one(os.fspath(root / alias), subdirectory, name)
KITCMD
}

# Refuse unsafe Claude integration targets, invalid settings, and a conflicting
# default-profile status line before a lifecycle transaction exists. Enrolled
# account profiles are preferences in their own right: their status lines are
# preserved later instead of making a machine-wide release flip fail.
preflight_claude_integration() {
  local source_root=$1 force=${2:-0} policy=${3:-install}
  local claude_root accounts_root
  claude_root=${SESSION_KIT_CLAUDE_HOME:-$HOME/.claude}
  accounts_root=${XDG_DATA_HOME:-$HOME/.local/share}/session-kit/accounts/claude
  python3 - "$source_root" "$HOME" "$claude_root" "$accounts_root" \
    "$claude_statusline_backups" "$claude_integration_ledger" "$force" "$policy" <<'PY'
import json
import os
import pathlib
import re
import stat
import sys

source_root, home, claude_root, accounts_root, backups_path, ledger_path = map(
    pathlib.Path, sys.argv[1:7]
)
force = sys.argv[7] == "1"
policy = sys.argv[8]


def owner_directory(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o002
    ):
        raise SystemExit(f"session-kit: Claude directory is not owner-controlled: {path}")
    return path


def safe_regular_target(path, label):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.geteuid()
    ):
        raise SystemExit(f"session-kit: unsafe {label}: {path}")


def load_settings(path):
    if owner_directory(path.parent) is None:
        return None
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {}
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        return None
    if info.st_uid != os.geteuid():
        return None
    if info.st_size > 1_048_576:
        return None
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(settings, dict):
        return None
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return None
    for event in ("SessionStart", "UserPromptSubmit", "Stop"):
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            return None
    return settings


for name in ("nameintent_title.sh", "statusline.sh"):
    source = source_root / name
    try:
        info = source.lstat()
    except FileNotFoundError:
        raise SystemExit("session-kit: release Claude integration files are unavailable")
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise SystemExit("session-kit: release Claude integration files are unavailable")

if not claude_root.is_absolute() or ".." in claude_root.parts:
    raise SystemExit("session-kit: Claude home must be an absolute normalized path")
try:
    home_exists = owner_directory(home)
except SystemExit:
    raise SystemExit("session-kit: HOME is not an owner-controlled directory")
if home_exists is None:
    raise SystemExit("session-kit: HOME is not an owner-controlled directory")
try:
    root_exists = owner_directory(claude_root)
except SystemExit:
    raise SystemExit(
        f"session-kit: Claude home is not an owner-controlled directory: {claude_root}"
    )
if root_exists is not None:
    hooks_root = claude_root / "hooks"
    if hooks_root.exists() or hooks_root.is_symlink():
        try:
            owner_directory(hooks_root)
        except SystemExit:
            raise SystemExit(
                f"session-kit: Claude hooks directory is not owner-controlled: {hooks_root}"
            )
    safe_regular_target(hooks_root / "nameintent_title.sh", "Claude integration file")
    safe_regular_target(claude_root / "statusline.sh", "Claude integration file")

safe_regular_target(backups_path, "Claude status-line backup")
safe_regular_target(ledger_path, "Claude integration ledger")
if backups_path.is_file():
    try:
        backups = json.loads(backups_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"session-kit: invalid Claude status-line backup: {backups_path}: {exc}; move that file aside to continue (its recorded status-line restores are lost to the kit)")
    if (
        not isinstance(backups, dict)
        or backups.get("schema_version") != 1
        or not isinstance(backups.get("entries"), dict)
    ):
        raise SystemExit(f"session-kit: invalid Claude status-line backup: {backups_path}; move that file aside to continue (its recorded status-line restores are lost to the kit)")
    for key, record in backups["entries"].items():
        if (
            not isinstance(key, str)
            or not pathlib.Path(key).is_absolute()
            or not isinstance(record, dict)
            or not isinstance(record.get("present"), bool)
            or set(record) - {"present", "value"}
            or (record["present"] and "value" not in record)
        ):
            raise SystemExit(
                f"session-kit: invalid Claude status-line backup: {backups_path}; "
                "move that file aside to continue (its recorded status-line restores are lost to the kit)"
            )

status_command = (
    "~/.claude/statusline.sh"
    if claude_root == home / ".claude"
    else str(claude_root / "statusline.sh")
)
canonical_status = {
    "type": "command",
    "command": status_command,
    "refreshInterval": 2,
}
default_settings = load_settings(claude_root / "settings.json")
if default_settings is not None:
    existing = default_settings.get("statusLine", object())
    if (
        "statusLine" in default_settings
        and existing != canonical_status
        and not force
        and policy == "install"
    ):
        raise SystemExit(
            "session-kit: refusing unrelated Claude statusLine in "
            f"{claude_root / 'settings.json'}; rerun with --force to preserve and replace it"
        )

try:
    profiles = sorted(os.scandir(accounts_root), key=lambda entry: entry.name)
except (FileNotFoundError, NotADirectoryError):
    profiles = []
except OSError as exc:
    raise SystemExit(f"session-kit: Claude account profiles are unreadable: {exc}")
for entry in profiles:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", entry.name):
        continue
    try:
        if not entry.is_dir(follow_symlinks=False):
            continue
    except OSError:
        continue
    profile = accounts_root / entry.name
    try:
        owner_directory(profile)
    except SystemExit:
        continue
    load_settings(profile / "settings.json")
PY
}

# Install the two Claude-side executables from the selected release and make
# their settings registrations exact. Other settings and other hooks are
# preserved. Existing enrolled Claude profiles need the same registration as
# the default profile because CLAUDE_CONFIG_DIR replaces, rather than extends,
# the provider config root.
install_claude_integration() {
  local release_id=$1 force=${2:-0} policy=${3:-install} source_root claude_root accounts_root
  source_root=$install_root/releases/$release_id/config/claude
  claude_root=${SESSION_KIT_CLAUDE_HOME:-$HOME/.claude}
  accounts_root=${XDG_DATA_HOME:-$HOME/.local/share}/session-kit/accounts/claude
  [[ -f $source_root/nameintent_title.sh && ! -L $source_root/nameintent_title.sh &&
     -f $source_root/statusline.sh && ! -L $source_root/statusline.sh ]] ||
    die "release Claude integration files are unavailable"
  python3 - "$source_root" "$HOME" "$claude_root" "$accounts_root" \
    "$claude_statusline_backups" "$claude_integration_ledger" "$force" \
    "$release_id" "$policy" <<'PY'
import hashlib
import json
import os
import pathlib
import re
import stat
import sys
import tempfile

source_root, home_raw, claude_raw, accounts_raw, backups_raw, ledger_raw = map(
    pathlib.Path, sys.argv[1:7]
)
force = sys.argv[7] == "1"
release_id = sys.argv[8]
policy = sys.argv[9]


def owner_directory(path, *, create=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if not create:
            return None
        path.mkdir(mode=0o700)
        info = path.lstat()
    mode = stat.S_IMODE(info.st_mode)
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.geteuid()
        or mode & 0o002
    ):
        return None
    return path


def atomic_bytes(destination, payload, mode):
    try:
        info = destination.lstat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid():
            raise SystemExit(f"session-kit: unsafe Claude integration file: {destination}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


home = pathlib.Path(home_raw)
claude_root = pathlib.Path(claude_raw)
if not claude_root.is_absolute() or ".." in claude_root.parts:
    raise SystemExit("session-kit: Claude home must be an absolute normalized path")
if owner_directory(home) is None:
    raise SystemExit("session-kit: HOME is not an owner-controlled directory")
if owner_directory(claude_root, create=True) is None:
    raise SystemExit(f"session-kit: Claude home is not an owner-controlled directory: {claude_root}")
hooks_root = owner_directory(claude_root / "hooks", create=True)
if hooks_root is None:
    raise SystemExit(f"session-kit: Claude hooks directory is not owner-controlled: {claude_root / 'hooks'}")

if claude_root == home / ".claude":
    hook_command = "~/.claude/hooks/nameintent_title.sh"
    status_command = "~/.claude/statusline.sh"
else:
    hook_command = str(claude_root / "hooks" / "nameintent_title.sh")
    status_command = str(claude_root / "statusline.sh")
owned_hook_commands = {
    "~/.claude/hooks/nameintent_title.sh",
    str(claude_root / "hooks" / "nameintent_title.sh"),
}
canonical_status = {
    "type": "command",
    "command": status_command,
    "refreshInterval": 2,
}


def load_settings(path):
    if owner_directory(path.parent) is None:
        return None
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {}, 0o600
    def skip(reason):
        print(
            f"session-kit: skipping unusable Claude settings file {path}: {reason}",
            file=sys.stderr,
        )
        return None

    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        return skip("not a regular file")
    if info.st_uid != os.geteuid():
        return skip("not owned by the current user")
    if info.st_size > 1_048_576:
        return skip("larger than 1 MiB")
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return skip(f"invalid JSON ({exc})")
    except (OSError, UnicodeError) as exc:
        return skip(f"cannot read as UTF-8 JSON ({exc})")
    if not isinstance(settings, dict):
        return skip("top level is not an object")
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return skip("hooks setting is not an object")
    for event in ("SessionStart", "UserPromptSubmit", "Stop"):
        if not isinstance(hooks.get(event, []), list):
            return skip(f"{event} hooks setting is not a list")
    return settings, stat.S_IMODE(info.st_mode)


def load_backups(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"schema_version": 1, "entries": {}}
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_size > 1_048_576
    ):
        raise SystemExit(f"session-kit: unsafe Claude status-line backup: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"session-kit: invalid Claude status-line backup: {path}: {exc}")
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or not isinstance(payload.get("entries"), dict)
    ):
        raise SystemExit(f"session-kit: invalid Claude status-line backup: {path}")
    for key, record in payload["entries"].items():
        if (
            not isinstance(key, str)
            or not pathlib.Path(key).is_absolute()
            or not isinstance(record, dict)
            or not isinstance(record.get("present"), bool)
            or set(record) - {"present", "value"}
            or (record["present"] and "value" not in record)
        ):
            raise SystemExit(f"session-kit: invalid Claude status-line backup: {path}")
    return payload

def write_settings(path, settings, mode, *, register_statusline):
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SystemExit(f"session-kit: Claude hooks setting must contain an object: {path}")
    canonical = {"hooks": [{"type": "command", "command": hook_command}]}
    for event in ("SessionStart", "UserPromptSubmit", "Stop"):
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise SystemExit(f"session-kit: Claude {event} hooks must contain a list: {path}")
        refreshed = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                refreshed.append(group)
                continue
            kept = [
                hook
                for hook in group["hooks"]
                if not (
                    isinstance(hook, dict)
                    and hook.get("type") == "command"
                    and hook.get("command") in owned_hook_commands
                )
            ]
            if kept or set(group) != {"hooks"}:
                copy = dict(group)
                copy["hooks"] = kept
                refreshed.append(copy)
        refreshed.append(canonical)
        hooks[event] = refreshed
    if register_statusline:
        settings["statusLine"] = canonical_status
    payload = (json.dumps(settings, indent=2, sort_keys=True) + "\n").encode()
    atomic_bytes(path, payload, mode)


settings_paths = [claude_root / "settings.json"]
accounts_root = pathlib.Path(accounts_raw)
try:
    account_entries = sorted(os.scandir(accounts_root), key=lambda entry: entry.name)
except FileNotFoundError:
    account_entries = []
except OSError as exc:
    raise SystemExit(f"session-kit: Claude account profiles are unreadable: {exc}")
for entry in account_entries:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", entry.name):
        continue
    account_root = accounts_root / entry.name
    try:
        if not entry.is_dir(follow_symlinks=False):
            continue
    except OSError:
        continue
    if owner_directory(account_root) is None:
        continue
    settings_paths.append(account_root / "settings.json")

loaded = []
for settings_path in settings_paths:
    item = load_settings(settings_path)
    if item is not None:
        loaded.append([settings_path, *item, True])

backups = load_backups(backups_raw)
entries = backups["entries"]
missing = object()
for item in loaded:
    settings_path, settings, _mode, _register_statusline = item
    existing = settings.get("statusLine", missing)
    if existing is not missing and existing != canonical_status and not force:
        item[3] = False
        print(
            "session-kit: skipping Claude statusLine rewrite in "
            f"{settings_path}; unrelated value preserved",
            file=sys.stderr,
        )
        continue
    key = os.fspath(settings_path)
    operator_value = existing is not missing and existing != canonical_status
    if operator_value or key not in entries:
        # Refresh any operator value that this run will replace. An exact kit
        # value from a pre-ledger release is kit ownership, not an operator
        # preimage; record it as absent only when there is no earlier record.
        record = {"present": operator_value}
        if operator_value:
            record["value"] = existing
        entries[key] = record

title_source = (source_root / "nameintent_title.sh").read_bytes()
status_source = (source_root / "statusline.sh").read_bytes()
atomic_bytes(hooks_root / "nameintent_title.sh", title_source, 0o700)
atomic_bytes(claude_root / "statusline.sh", status_source, 0o700)
atomic_bytes(
    backups_raw,
    (json.dumps(backups, indent=2, sort_keys=True) + "\n").encode(),
    0o600,
)
ledger = {
    "schema_version": 1,
    "release_id": release_id,
    "files": {
        os.fspath(hooks_root / "nameintent_title.sh"): hashlib.sha256(title_source).hexdigest(),
        os.fspath(claude_root / "statusline.sh"): hashlib.sha256(status_source).hexdigest(),
    },
}
atomic_bytes(
    ledger_raw,
    (json.dumps(ledger, indent=2, sort_keys=True) + "\n").encode(),
    0o600,
)
for settings_path, settings, mode, register_statusline in loaded:
    write_settings(
        settings_path,
        settings,
        mode,
        register_statusline=register_statusline,
    )
PY
}

# Releases installed before the Claude integration payload was introduced are
# valid rollback targets.  A wholly absent payload is that legacy shape; a
# partial payload is corruption and must still fail closed.
release_has_claude_integration() {
  local release_id=$1 source_root
  source_root=$install_root/releases/$release_id/config/claude
  if [[ -f $source_root/nameintent_title.sh && ! -L $source_root/nameintent_title.sh &&
        -f $source_root/statusline.sh && ! -L $source_root/statusline.sh ]]; then
    return 0
  fi
  if [[ ! -e $source_root/nameintent_title.sh && ! -L $source_root/nameintent_title.sh &&
        ! -e $source_root/statusline.sh && ! -L $source_root/statusline.sh ]]; then
    return 1
  fi
  die "release Claude integration payload is incomplete or unsafe"
}

# The lock-generation fence is a code capability, not a release-number
# boundary.  A retained release is fence-aware only when its StateLock carries
# the complete generation-2 path that recognizes, publishes, and enters through
# the versioned publishing lock.
release_has_inventory_lock_fence() {
  local release_id=$1
  python3 - "$install_root/releases/$release_id/lib/sessionkit_inventory/state_io.py" <<'PY'
import ast
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
try:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
except (OSError, UnicodeError, SyntaxError):
    raise SystemExit(1)

constants = {}
state_lock = None
for node in tree.body:
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        for target in targets:
            if isinstance(target, ast.Name) and isinstance(value, ast.Constant):
                constants[target.id] = value.value
    elif isinstance(node, ast.ClassDef) and node.name == "StateLock":
        state_lock = node

if state_lock is None:
    raise SystemExit(1)
methods = {
    node.name: node
    for node in state_lock.body
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
}
enter = methods.get("__enter__")
enters_versioned_lock = enter is not None and any(
    isinstance(node, ast.Attribute)
    and node.attr == "_open_versioned_publishing_lock"
    for node in ast.walk(enter)
)
aware = (
    constants.get("_PUBLISHING_LOCK_NAME") == "inventory-v2.lock"
    and constants.get("_LEGACY_PUBLISHING_LOCK_NAME") == "inventory.lock"
    and constants.get("_LEGACY_LOCK_FENCE")
    == b"session-kit publishing lock generation 2\n"
    and "_legacy_lock_is_fenced" in methods
    and "_publish_legacy_lock_fence" in methods
    and "_open_versioned_publishing_lock" in methods
    and enters_versioned_lock
)
raise SystemExit(0 if aware else 1)
PY
}

# A rollback to code that only knows inventory.lock must remove the exact
# generation-2 refusal sentinel before `current` can name that code. Serialize
# the durable rollback hold, un-fence, and pointer flip under one versioned-lock
# hold. A generation-two publisher that was already running sees the hold after
# it wakes and refuses instead of re-fencing legacy code.
unfence_inventory_lock_and_select_rollback() {
  local target=$1
  python3 - "$state_root" "$install_root/releases/$target" "$install_root/current" <<'PY'
import contextlib
import fcntl
import os
import pathlib
import signal
import stat
import sys
import tempfile
import uuid

root, target, current = map(pathlib.Path, sys.argv[1:4])
fence = b"session-kit publishing lock generation 2\n"
rollback_hold = b"session-kit legacy rollback selected\n"
legacy = root / "inventory.lock"
versioned = root / "inventory-v2.lock"
hold = root / "inventory-rollback-v2"


def failpoint(name):
    if (
        os.environ.get("SESSION_KIT_TESTING") == "1"
        and os.environ.get("SESSION_KIT_TEST_FAILPOINT") == name
    ):
        parent = os.getppid()
        if os.environ.get("SESSION_KIT_TEST_FAILPOINT_MODE") == "kill":
            os.kill(parent, signal.SIGKILL)
            os._exit(137)
        raise SystemExit(f"session-kit: isolated test failpoint after {name}")


def fsync_directory(path):
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def atomic_regular(path, mode, content):
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=root)
    try:
        os.fchmod(descriptor, mode)
        if content:
            os.write(descriptor, content)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        fsync_directory(root)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def exact_hold_or_missing():
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(hold, flags)
    except FileNotFoundError:
        return False
    try:
        opened = os.fstat(descriptor)
        pathname = hold.lstat()
        body = os.read(descriptor, len(rollback_hold) + 1)
    finally:
        os.close(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or stat.S_IMODE(opened.st_mode) != 0o400
        or opened.st_uid != os.geteuid()
        or opened.st_nlink != 1
        or (opened.st_dev, opened.st_ino) != (pathname.st_dev, pathname.st_ino)
        or body != rollback_hold
    ):
        raise SystemExit(
            f"session-kit: legacy rollback publication hold is unsafe: {hold}"
        )
    return True

try:
    root_info = root.lstat()
except OSError as exc:
    raise SystemExit(f"session-kit: cannot inspect inventory state directory: {exc}")
if (
    not stat.S_ISDIR(root_info.st_mode)
    or stat.S_ISLNK(root_info.st_mode)
    or stat.S_IMODE(root_info.st_mode) != 0o700
    or root_info.st_uid != os.geteuid()
):
    raise SystemExit(
        f"session-kit: inventory state directory is unsafe for rollback: {root}"
    )

flags = os.O_RDWR | os.O_CREAT
if hasattr(os, "O_CLOEXEC"):
    flags |= os.O_CLOEXEC
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
try:
    lock_fd = os.open(versioned, flags, 0o600)
    opened = os.fstat(lock_fd)
    pathname = versioned.lstat()
    if (
        not stat.S_ISREG(opened.st_mode)
        or stat.S_IMODE(opened.st_mode) != 0o600
        or opened.st_uid != os.geteuid()
        or opened.st_nlink != 1
        or (opened.st_dev, opened.st_ino)
        != (pathname.st_dev, pathname.st_ino)
    ):
        raise OSError(f"unsafe generation-2 inventory lock: {versioned}")
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    pathname = versioned.lstat()
    if (opened.st_dev, opened.st_ino) != (pathname.st_dev, pathname.st_ino):
        raise OSError(f"generation-2 inventory lock changed: {versioned}")
except OSError as exc:
    if "lock_fd" in locals():
        with contextlib.suppress(OSError):
            os.close(lock_fd)
    raise SystemExit(f"session-kit: cannot serialize inventory un-fence: {exc}")

try:
    read_flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        read_flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        read_flags |= os.O_NOFOLLOW
    try:
        legacy_fd = os.open(legacy, read_flags)
    except FileNotFoundError:
        legacy_fd = None
        legacy_info = None
        legacy_body = None
    except OSError as exc:
        raise SystemExit(f"session-kit: cannot inspect legacy inventory lock: {exc}")
    else:
        try:
            legacy_info = os.fstat(legacy_fd)
            legacy_path_info = legacy.lstat()
            legacy_body = os.read(legacy_fd, len(fence) + 1)
        except OSError as exc:
            raise SystemExit(
                f"session-kit: cannot inspect legacy inventory lock: {exc}"
            )
        finally:
            os.close(legacy_fd)
        if (
            not stat.S_ISREG(legacy_info.st_mode)
            or stat.S_ISLNK(legacy_info.st_mode)
            or legacy_info.st_uid != os.geteuid()
            or legacy_info.st_nlink != 1
            or (legacy_info.st_dev, legacy_info.st_ino)
            != (legacy_path_info.st_dev, legacy_path_info.st_ino)
        ):
            raise SystemExit(
                f"session-kit: legacy inventory lock is unsafe for rollback: {legacy}"
            )
        legacy_mode = stat.S_IMODE(legacy_info.st_mode)
        if legacy_mode not in (0o400, 0o600) or (
            legacy_mode == 0o400 and legacy_body != fence
        ):
            raise SystemExit(
                f"session-kit: legacy inventory lock is not the exact generation-2 fence: {legacy}"
            )

    if not exact_hold_or_missing():
        atomic_regular(hold, 0o400, rollback_hold)
    failpoint("rollback-fence-marked")

    if legacy_info is None or stat.S_IMODE(legacy_info.st_mode) != 0o600:
        atomic_regular(legacy, 0o600, b"")
    failpoint("rollback-unfenced")

    temporary_link = current.with_name(
        f".{current.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        os.symlink(target, temporary_link)
        os.replace(temporary_link, current)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary_link)
finally:
    with contextlib.suppress(OSError):
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    os.close(lock_fd)
PY
}

install_defaults() {
  local release_id=$1 force=${2:-0}
  mkdir -p "$config_root"
  chmod 700 "$config_root" 2>/dev/null || true
  if [[ ! -e $config_root/projects.tsv ]]; then
    install -m 0600 "$install_root/releases/$release_id/config/projects.example.tsv" \
      "$config_root/projects.tsv"
    projects_created=1
  fi
  [[ -e $config_root/inventory.json ]] ||
    install -m 0600 "$install_root/releases/$release_id/config/session-inventory.example.json" \
      "$config_root/inventory.json"
  if [[ ! -e $shpool_config ]]; then
    mkdir -p "$(dirname -- "$shpool_config")"
    install -m 0600 "$install_root/releases/$release_id/config/shpool.example.toml" \
      "$shpool_config"
  fi
  if [[ $(platform) == macos ]]; then
    configure_macos_shpool_shell
  fi
  # Kit-owned sk-*.tmTheme files track the release. Other themes are untouched.
  install_codex_themes "$release_id"
  # The kit-owned Codex tab title template. The person's config.toml is never
  # edited; kit sessions pass this value on their own command line.
  install_codex_terminal_title "$release_id"
  # The /kit verb, in each provider's own command directory.
  install_provider_commands "$release_id"
  # The title hook and live status line, including every provider profile's
  # registration, track the selected release as one transaction.
  install_claude_integration "$release_id" "$force"
}

configure_macos_shpool_shell() {
  local modern_bash
  modern_bash=$(command -v bash)
  python3 - "$shpool_config" "$modern_bash" <<'PY'
import os
import pathlib
import stat
import sys
import tempfile
import tomllib

path = pathlib.Path(sys.argv[1])
shell = pathlib.Path(sys.argv[2]).resolve(strict=True)
info = path.lstat()
if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
    raise SystemExit("session-kit: shpool config must be an owner-controlled regular file")
text = path.read_text(encoding="utf-8")
try:
    value = tomllib.loads(text)
except tomllib.TOMLDecodeError as exc:
    raise SystemExit(f"session-kit: shpool config is invalid: {exc}")
configured = value.get("shell")
if configured is not None and configured != str(shell):
    raise SystemExit(
        f"session-kit: shpool config selects {configured!r}; macOS requires {str(shell)!r}"
    )
if configured == str(shell):
    raise SystemExit(0)
escaped = str(shell).replace("\\", "\\\\").replace('"', '\\"')
updated = f'shell = "{escaped}"\n' + text
fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
try:
    os.fchmod(fd, stat.S_IMODE(info.st_mode))
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(updated)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
finally:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
PY
}

install_systemd_units() {
  local release_id=$1
  [[ $(platform) == linux ]] || return 0
  mkdir -p "$service_root"
  local unit shpool_path source
  # The unit list comes from the RUNNING kit, and during a rollback that is
  # still the release being left -- so it correctly names units the TARGET
  # release never carried. Copying one of those aborted the whole transaction
  # under `set -euo pipefail`, which meant that the day any release added a
  # unit, every earlier release became unreachable: adding a unit removed the
  # undo for adding it. The list is right and the file is missing, so a
  # missing file is skipped and said, never fatal.
  #
  # The same loop prunes in the other direction. A unit file left in the
  # service root by a newer release is not merely untidy: its program is the
  # release that is now current, and that program does not understand the
  # arguments the stale unit passes it, so systemd would run a failing job on
  # a timer forever. Nothing else prunes this directory.
  for unit in "${systemd_units[@]}"; do
    # A unit is a plain file name inside the service root. Both halves of this
    # loop take the name from the RUNNING kit and join it onto a directory, so
    # a name carrying a path would copy a file in from outside the release and
    # -- worse, since it deletes -- prune a file from outside $service_root. No
    # real release list holds such a name; refusing it costs nothing, and the
    # refusal is not fatal for the same reason a missing file is not: this loop
    # must never be the thing that makes a release unreachable.
    if [[ -z $unit || $unit != "${unit##*/}" || $unit == .* ]]; then
      printf 'session-kit: refusing unsafe unit name: %s\n' "$unit" >&2
      continue
    fi
    source=$install_root/releases/$release_id/systemd/$unit
    if [[ ! -f $source ]]; then
      if [[ -f $service_root/$unit && ! -L $service_root/$unit ]]; then
        command rm -f -- "$service_root/$unit"
        printf 'session-kit: %s is not part of this release; its unit file was removed. Disable it with: systemctl --user disable %s\n' \
          "$unit" "$unit" >&2
      else
        printf 'session-kit: %s is not part of this release; skipped.\n' "$unit" >&2
      fi
      continue
    fi
    install -m 0644 "$source" "$service_root/$unit"
  done
  # Templating an absent file is the same fatality by another route: a release
  # that does not carry shpool.service leaves nothing here to fill in, and the
  # loop above has just made that state survivable. Reading it unconditionally
  # would put the abort straight back into the same transaction.
  [[ -f $service_root/shpool.service ]] || return 0
  shpool_path=$(command -v shpool)
  python3 - "$service_root/shpool.service" "$shpool_path" <<'PY'
import os
import pathlib
import sys
import tempfile

path = pathlib.Path(sys.argv[1])
executable = sys.argv[2]
if any(character.isspace() for character in executable):
    raise SystemExit("session-kit: shpool path contains whitespace; refusing systemd unit")
text = path.read_text(encoding="utf-8")
if text.count("@SHPOOL@") != 1:
    raise SystemExit("session-kit: shpool service template is invalid")
text = text.replace("@SHPOOL@", executable)
fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
try:
    os.fchmod(fd, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
finally:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
PY
}

install_launchd_units() {
  local release_id=$1
  [[ $(platform) == macos ]] || return 0
  local shpool_path modern_bash_path
  shpool_path=$(command -v shpool)
  modern_bash_path=$(command -v bash)
  [[ $(shpool version 2>/dev/null) == "shpool 0.11.0" ]] ||
    die "macOS requires shpool 0.11.0"
  python3 - "$shpool_path" <<'PY'
import os,pathlib,stat,sys
path=pathlib.Path(sys.argv[1])
info=path.stat()
if (
    not path.is_absolute()
    or not stat.S_ISREG(info.st_mode)
    or info.st_uid != os.geteuid()
    or info.st_mode & 0o022
    or not os.access(path, os.X_OK)
):
    raise SystemExit("session-kit: shpool must be an owner-controlled, non-writable executable")
PY
  mkdir -p "$launchd_template_root" "$state_root/logs"
  chmod 700 "$state_root" "$state_root/logs" 2>/dev/null || true
  python3 - "$launchd_template_root" "$shpool_path" "$bin_dir/shpool_reaper" \
    "$install_root/current/bin/session_kit_watchdog" "$state_root/logs" \
    "$install_root" "$config_root" "$state_root" "$bin_dir" \
    "${XDG_CONFIG_HOME:-$HOME/.config}" "${XDG_STATE_HOME:-$HOME/.local/state}" \
    "$modern_bash_path" "$shpool_config" <<'PY'
import os
import pathlib
import plistlib
import sys
import tempfile

(
    root, shpool, reaper, watchdog, logs, install_root, config_root,
    state_root, bin_dir, xdg_config, xdg_state, modern_bash, shpool_config,
) = map(pathlib.Path, sys.argv[1:])
for executable in (shpool, reaper, watchdog):
    if not executable.is_absolute():
        raise SystemExit(f"session-kit: launchd executable must be absolute: {executable}")
path_value = ":".join(
    value for value in (
        str(pathlib.Path.home() / ".local/bin"),
        str(pathlib.Path.home() / ".cargo/bin"),
        "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
    )
)
specs = {
    "com.session-kit.shpool.plist": {
        "Label": "com.session-kit.shpool",
        "ProgramArguments": [str(shpool), "--config-file", str(shpool_config), "daemon"],
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 10,
    },
    "com.session-kit.reaper.plist": {
        "Label": "com.session-kit.reaper",
        "ProgramArguments": [str(reaper), "--auto-close"],
        "StartInterval": 3600,
    },
    "com.session-kit.watchdog.plist": {
        "Label": "com.session-kit.watchdog",
        "ProgramArguments": [str(watchdog), "--once"],
        "StartInterval": 60,
        "EnvironmentVariables": {"SESSION_KIT_WATCHDOG_MODE": "report"},
    },
}
for filename, value in specs.items():
    label = value["Label"]
    value.setdefault("EnvironmentVariables", {})["PATH"] = path_value
    value["EnvironmentVariables"]["HOME"] = str(pathlib.Path.home())
    value["EnvironmentVariables"]["SHELL"] = str(modern_bash)
    value["EnvironmentVariables"].update({
        "SESSION_KIT_ROOT": str(install_root),
        "SESSION_KIT_CONFIG_ROOT": str(config_root),
        "SESSION_KIT_STATE_DIR": str(state_root),
        "SESSION_KIT_BIN_DIR": str(bin_dir),
        "XDG_CONFIG_HOME": str(xdg_config),
        "XDG_STATE_HOME": str(xdg_state),
    })
    value["WorkingDirectory"] = str(pathlib.Path.home())
    value["StandardOutPath"] = str(logs / f"{label}.out.log")
    value["StandardErrorPath"] = str(logs / f"{label}.err.log")
    for key in ("StandardOutPath", "StandardErrorPath"):
        log_path = pathlib.Path(value[key])
        descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(descriptor)
        os.chmod(log_path, 0o600)
    path = root / filename
    fd, temporary = tempfile.mkstemp(prefix=f".{filename}.", dir=root)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb") as handle:
            plistlib.dump(value, handle, fmt=plistlib.FMT_XML, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
PY
  local unit
  for unit in "${launchd_units[@]}"; do
    plutil -lint "$launchd_template_root/$unit" >/dev/null || die "invalid launchd plist: $unit"
  done
}

install_platform_services() {
  if [[ $(platform) == macos ]]; then
    install_launchd_units "$1"
  else
    install_systemd_units "$1"
  fi
  refresh_platform_services
}

# One read-only-to-the-estate systemctl call, over the same seam the tests use.
# A test install must never reach the operator's own user manager, so under the
# test flag nothing runs unless a fixture command is named explicitly.
kit_systemctl() {
  local tool=${SESSION_KIT_SYSTEMCTL_CMD:-systemctl}
  if [[ ${SESSION_KIT_TESTING:-0} == 1 && -z ${SESSION_KIT_SYSTEMCTL_CMD:-} ]]; then
    return 0
  fi
  command -v "$tool" >/dev/null 2>&1 || return 0
  "$tool" --user "$@"
}

# Units are files; a running process is not. An activation writes new unit files
# and moves `current`, and until systemd is told it keeps serving the PREVIOUS
# unit definitions -- while bash, holding the inode it exec'd, keeps running the
# previous release's code. Ten activations passed over one watchdog process
# without a single restart, so every watchdog fix shipped that day was inert and
# `sp doctor` reported green throughout. The unit file said so in a comment and
# nothing enforced it; this is the enforcement.
#
# The session daemon and its socket are never restarted here: that would end
# every managed session. Only kit processes that carry their own code are, and
# only when they are already running -- try-restart starts nothing that the
# operator has not enabled.
refresh_platform_services() {
  local unit
  if [[ $(platform) == macos ]]; then
    for unit in "${launchd_restart_units[@]}"; do
      [[ -f $service_root/$unit && ! -L $service_root/$unit ]] || continue
      launchctl kickstart -k "gui/$UID/${unit%.plist}" >/dev/null 2>&1 ||
        printf 'session-kit: %s was not restarted; it keeps running the previous release until it is\n' \
          "${unit%.plist}" >&2
    done
    return 0
  fi
  [[ $(platform) == linux ]] || return 0
  if ! kit_systemctl daemon-reload >/dev/null 2>&1; then
    printf 'session-kit: systemd did not reload the units; run: systemctl --user daemon-reload\n' >&2
    return 0
  fi
  for unit in "${systemd_timer_units[@]:-}"; do
    [[ -n $unit ]] || continue
    [[ -f $service_root/$unit && ! -L $service_root/$unit ]] || continue
    # `enable` only when systemd has never been told about this unit at all.
    # A timer the operator disabled reports `disabled`, and that answer is
    # their -- an update must not quietly turn it back on.
    state=$(kit_systemctl is-enabled "$unit" 2>/dev/null || true)
    [[ -z $state ]] || continue
    kit_systemctl enable --now "$unit" >/dev/null 2>&1 ||
      printf 'session-kit: %s was installed but not enabled; enable it with: systemctl --user enable --now %s\n' \
        "$unit" "$unit" >&2
  done
  for unit in "${systemd_restart_units[@]}"; do
    [[ -f $service_root/$unit && ! -L $service_root/$unit ]] || continue
    kit_systemctl try-restart "$unit" >/dev/null 2>&1 ||
      printf 'session-kit: %s was not restarted; it keeps running the previous release until it is; restart it with: systemctl --user restart %s\n' \
        "$unit" "$unit" >&2
  done
  return 0
}

write_release_manifest() {
  python3 - "$1" "$2" <<'PY'
import hashlib
import json
import os
import pathlib
import stat
import sys

root = pathlib.Path(sys.argv[1])
commit = sys.argv[2]
meta = root / "RELEASE.json"
meta.write_text(
    json.dumps(
        {
            "schema_version": 1,
            "lifecycle_schema_version": 2,
            "commit": commit,
        },
        indent=2,
        sort_keys=True,
    ) + "\n",
    encoding="utf-8",
)
for path in sorted(root.rglob("*"), reverse=True):
    if path.is_symlink():
        raise SystemExit(f"session-kit: release source contains symlink: {path}")
    if path.is_file():
        mode = stat.S_IMODE(path.stat().st_mode)
        os.chmod(path, 0o555 if mode & 0o111 else 0o444)
lines = []
for path in sorted((path for path in root.rglob("*") if path.is_file())):
    rel = path.relative_to(root).as_posix()
    lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {rel}")
manifest = root / "MANIFEST.sha256"
manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
os.chmod(manifest, 0o444)
for path in sorted((path for path in root.rglob("*") if path.is_dir()), reverse=True):
    os.chmod(path, 0o555)
PY
}

# Bytecode caches have to belong to the tree they ship in. `cp -R` gives every
# copied file a new mtime, so a cache built while the SOURCE tree was in use
# records a source timestamp the copy does not have, and python then recompiles
# from source on every import -- measured at 112-148 ms where a valid cache
# costs 41 ms, on every `sp` command, every picker frame helper and every
# five-second refresh. Nothing could repair it either: the release directory is
# read-only, and shpool_status exports PYTHONDONTWRITEBYTECODE=1 for that exact
# reason, so the failed cache write was silent and permanent.
#
# Compiling here, from the copied sources, with hash-based invalidation, makes
# the cache valid however the mtimes land -- including after a rollback that
# restores a file, and after any copy that renews a timestamp. A machine without
# compileall still installs; it just pays the compile per call, as it does today.
compile_release_bytecode() {
  local staging=$1
  [[ -d $staging/lib ]] || return 0
  # A cache inherited from the source tree is not evidence about this tree.
  find "$staging" -type d -name __pycache__ -prune -exec rm -rf -- {} + \
    2>/dev/null || true
  python3 -m compileall -q --invalidation-mode checked-hash -- "$staging/lib" \
    >/dev/null 2>&1 ||
    printf 'session-kit: bytecode caches were not built; python compiles from source on every call\n' >&2
  return 0
}

install_release() {
  local source=$1 release_id=$2
  local release_dir=$install_root/releases/$release_id
  if [[ -e $release_dir ]]; then
    [[ -d $release_dir && ! -L $release_dir ]] ||
      die "release path is unsafe: $release_dir"
    verify_release "$release_dir" "$release_id"
    return 0
  fi
  mkdir -p "$install_root/releases"
  local staging=$install_root/releases/.install-"$release_id"-$$
  [[ ! -e $staging ]] || die "staging path already exists"
  mkdir "$staging"
  local item
  for item in bin lib bashrc config deploy systemd macos shpool-patch extras; do
    [[ -e $source/$item ]] || continue
    cp -R "$source/$item" "$staging/$item"
  done
  find "$staging/bin" "$staging/deploy" -type f -exec chmod 0755 {} +
  compile_release_bytecode "$staging"
  write_release_manifest "$staging" "$release_id"
  mv "$staging" "$release_dir"
  chmod 0555 "$release_dir"
  verify_release "$release_dir" "$release_id"
}

install_command() {
  local source= login=prompt journal=prompt projects=prompt noninteractive=0 force=0
  while (($#)); do
    case "$1" in
      --source) require_arg "$1" "${2:-}"; source=$2; shift 2 ;;
      --enable-login) login=enable; shift ;;
      --disable-login) login=disable; shift ;;
      --journal) require_arg "$1" "${2:-}"; journal=$2; shift 2 ;;
      --import-projects) projects=import; shift ;;
      --no-import-projects) projects=skip; shift ;;
      --force) force=1; shift ;;
      --non-interactive)
        noninteractive=1
        shift
        ;;
      *) die "unknown install option: $1" ;;
    esac
  done
  [[ -n $source ]] || die "install requires --source PATH"
  recover_pending_transaction
  source=$(cd -- "$source" && pwd -P) || die "source is unavailable"
  check_source "$source"
  local release_id previous current_platform
  release_id=$(release_id_from_source "$source")
  previous=$(current_release_id)
  current_platform=$(platform)
  if [[ $login == prompt ]]; then
    # The installer asks nothing. Login integration is the marked default, so
    # a person's install takes it and a rollover over an existing installation
    # with a validated marker keeps it; the summary at the end states the fact
    # and names the one command that turns it off. Disabling (which also
    # invalidates the launch marker) stays an explicit --disable-login choice,
    # never a silent default, and an unattended install adds nothing to a
    # shell it was not asked to touch.
    if [[ -t 0 && $noninteractive == 0 ]] ||
       [[ -L $install_root/current && -f $integration_marker &&
          ! -L $integration_marker ]]; then
      login=enable
    else
      login=disable
    fi
  fi
  if [[ $journal == prompt ]]; then
    if [[ -L $install_root/current ]]; then
      # A rollover over an existing installation keeps the operator's current
      # journal choice, exactly like the login rollover above. Turning
      # journals off must be an explicit --journal off, never a silent
      # default: the silent off-default erased all terminal recordings from
      # 2026-07-30 to 2026-08-12 and re-armed itself the same afternoon.
      journal=preserve
    else
      # Session history can hold raw commands and output, so recording stays
      # off unless it is asked for. The summary at the end states the fact and
      # names the way on; automation chooses with --journal on|off.
      journal=off
    fi
  fi
  validate_mutable_targets
  if [[ -e $install_root/releases/$release_id ]]; then
    verify_release "$install_root/releases/$release_id" "$release_id"
  fi
  preflight_claude_integration "$source/config/claude" "$force" install
  python3 "$source/deploy/session-kit-release" seed-floor \
    --state-root "$state_root" \
    --remedy-tool "$source/tools/reset-collection-order.py" >/dev/null
  begin_transaction install
  install_transaction_pending=1
  trap 'if [[ ${install_transaction_pending:-0} == 1 ]]; then recover_pending_transaction || true; fi' EXIT
  install_release "$source" "$release_id"
  # Management stays on the newest successfully verified release even when
  # the user later rolls the session payload back. This recovery pointer is
  # deliberately independent of the selected runtime release.
  atomic_symlink "$install_root/releases/$release_id" "$manager_link"
  atomic_symlink "$install_root/releases/$release_id" "$install_root/current"
  lifecycle_failpoint current
  install_launchers "$release_id"
  install_completions "$release_id"
  install_defaults "$release_id" "$force"
  lifecycle_failpoint themes
  configure_initial_projects "$projects" "$noninteractive"
  install_platform_services "$release_id"
  set_journal_choice "$journal"
  if [[ $login == enable ]]; then enable_login --quiet; else disable_login --quiet; fi
  write_ownership_markers
  write_receipt "$release_id" "$source" "$previous" "$current_platform"
  commit_transaction
  install_transaction_pending=0
  trap - EXIT
  printf 'Installed Session Kit release %s.\n' "$release_id"
  if [[ -f $integration_marker && ! -L $integration_marker ]]; then
    printf 'Login integration is on. Turn it off with: session-kit disable-login\n'
  else
    printf 'Login integration is off. Turn it on with: session-kit enable-login\n'
  fi
  if [[ -e $HOME/.no_shpool_journal ]]; then
    printf 'History recording is off. Turn it on with: session-kit update --journal on\n'
  else
    printf 'History recording is on. Turn it off with: session-kit update --journal off\n'
  fi
  if [[ $current_platform == linux ]]; then
    printf 'User services were installed and systemd was reloaded. Start them with:\n'
    printf '  systemctl --user enable --now shpool.socket shpool-reaper.timer session-kit-subagent-sweep.timer session-kit-watchdog.service\n'
  elif [[ $current_platform == macos ]]; then
    printf 'LaunchAgents were installed but not loaded. Review them, then run:\n'
    printf '  session-kit services enable\n'
  fi
  printf 'Run session-kit doctor before opening a new session.\n'
}

update_command() {
  # journal defaults to preserve: an update must never flip recording off
  # (or on) behind the operator's back, see install_command's rollover note.
  local source= login= journal=preserve force=0
  while (($#)); do
    case "$1" in
      --source) require_arg "$1" "${2:-}"; source=$2; shift 2 ;;
      --enable-login) login=enable; shift ;;
      --disable-login) login=disable; shift ;;
      --journal) require_arg "$1" "${2:-}"; journal=$2; shift 2 ;;
      --force) force=1; shift ;;
      *) die "unknown update option: $1" ;;
    esac
  done
  [[ $(platform) != unsupported ]] || die "update is supported only on Linux and macOS"
  # Recovery is independent of the next source path. Restore a prior interrupted
  # transaction before validating a new or now-missing source.
  recover_pending_transaction
  if [[ -z $source && -r $receipt_path ]]; then
    source=$(python3 - "$receipt_path" <<'PY'
import json,sys
try:
    value=json.load(open(sys.argv[1],encoding="utf-8")).get("source","")
except (OSError,ValueError):
    value=""
print(value if isinstance(value,str) else "")
PY
)
  fi
  [[ -n $source ]] || die "update needs --source PATH"
  source=$(cd -- "$source" && pwd -P) || die "source is unavailable"
  check_source "$source"
  if [[ -z $login ]]; then
    if [[ -f $integration_marker && ! -L $integration_marker ]]; then
      login=enable
    else
      login=disable
    fi
  fi
  local handoff_args=(--source "$source" --journal "$journal")
  case "$login" in
    enable) handoff_args+=(--enable-login) ;;
    disable) handoff_args+=(--disable-login) ;;
  esac
  (( force == 0 )) || handoff_args+=(--force)
  if [[ ${SESSION_KIT_UPDATE_HANDOFF:-0} != 1 ]]; then
    [[ -f $source/bin/session-kit && ! -L $source/bin/session-kit &&
       -x $source/bin/session-kit ]] || die "source management command is unsafe"
    SESSION_KIT_UPDATE_HANDOFF=1 exec "$source/bin/session-kit" update "${handoff_args[@]}"
  fi
  local args=("${handoff_args[@]}" --non-interactive --no-import-projects)
  install_command "${args[@]}"
}

rollback_command() {
  local target=
  while (($#)); do
    case "$1" in
      --to) require_arg "$1" "${2:-}"; target=$2; shift 2 ;;
      *) die "unknown rollback option: $1" ;;
    esac
  done
  [[ $(platform) != unsupported ]] || die "rollback is supported only on Linux and macOS"
  recover_pending_transaction
  if [[ -z $target && -r $receipt_path ]]; then
    target=$(python3 - "$receipt_path" <<'PY'
import json,sys
try:
    value=json.load(open(sys.argv[1],encoding="utf-8")).get("previous_release","")
except (OSError,ValueError):
    value=""
print(value if isinstance(value,str) else "")
PY
)
  fi
  [[ $target =~ ^[0-9a-f]{40}$ ]] || die "rollback requires a full release commit"
  [[ -d $install_root/releases/$target && ! -L $install_root/releases/$target ]] ||
    die "rollback release is not installed: $target"
  verify_release "$install_root/releases/$target" "$target"
  local rollback_unfence=0
  if ! release_has_inventory_lock_fence "$target"; then
    rollback_unfence=1
  fi
  local active_tool=$install_root/current/deploy/session-kit-release
  if [[ -f $active_tool && ! -L $active_tool ]]; then
    python3 - "$active_tool" "$HOME" "$install_root/releases/$target" <<'PY'
import inspect
import pathlib
import runpy
import sys

namespace = runpy.run_path(sys.argv[1], run_name="session_kit_release_gate")
gate = namespace["validate_rollback_launch_records"]
# An older active tool has the one-argument legacy-only gate; a target-aware
# tool must be told the target or it refuses every current-family record.
if len(inspect.signature(gate).parameters) >= 2:
    gate(pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]))
else:
    gate(pathlib.Path(sys.argv[2]))
PY
  fi
  validate_mutable_targets
  local rollback_claude=0
  if release_has_claude_integration "$target"; then
    preflight_claude_integration \
      "$install_root/releases/$target/config/claude" 0 rollback
    rollback_claude=1
  fi
  begin_transaction rollback
  # A rollback may select a release that predates provider-hook activation and
  # therefore cannot interpret this release's expanded transaction journal.
  # Keep recovery pinned to the code that began the flip and restore the whole
  # preimage on every ordinary failure before an older launcher can run.
  rollback_transaction_pending=1
  trap 'if [[ ${rollback_transaction_pending:-0} == 1 ]]; then recover_pending_transaction || true; fi' EXIT
  local previous source=
  previous=$(current_release_id)
  if [[ -r $receipt_path ]]; then
    source=$(python3 - "$receipt_path" <<'PY'
import json,sys
try:
    value=json.load(open(sys.argv[1],encoding="utf-8")).get("source","")
except (OSError,ValueError):
    value=""
print(value if isinstance(value,str) else "")
PY
)
  fi
  if (( rollback_unfence )); then
    unfence_inventory_lock_and_select_rollback "$target"
  else
    atomic_symlink "$install_root/releases/$target" "$install_root/current"
  fi
  lifecycle_failpoint current
  install_codex_themes "$target"
  # Pre-integration releases have no config/claude payload.  They remain valid
  # emergency rollback targets; keep the currently installed Claude-side
  # integration in place until a release carrying that payload is selected.
  if (( rollback_claude )); then
    install_claude_integration "$target" 0 rollback
  fi
  install_codex_terminal_title "$target"
  lifecycle_failpoint themes
  install_platform_services "$target"
  if [[ -f $integration_marker && ! -L $integration_marker ]]; then
    printf 'session-kit-integration-v1 %s\n' "$target" > "$integration_marker"
    chmod 600 "$integration_marker"
  fi
  write_ownership_markers
  write_receipt "$target" "$source" "$previous" "$(platform)"
  # Refresh every stable launcher while the recovery journal still exists.
  # The management launcher pins that journal to this pre-flip release, so it
  # remains capable of restoring the transaction even though `current` now
  # names the rollback target.
  install_launchers "$target"
  install_completions "$target"
  lifecycle_failpoint rollback-precommit
  commit_transaction
  rollback_transaction_pending=0
  trap - EXIT
  # Stable launchers already resolve `current`, and their management entry pins
  # recovery to the journal preimage while a transaction is pending. No
  # post-commit launcher rewrite exists, so there is no kill window between
  # journal removal and recovery-capable command installation.
  lifecycle_failpoint rollback-postcommit
  lifecycle_failpoint rollback-launchers
  printf 'Rolled back Session Kit to %s. Sessions kept running; a running watchdog was restarted onto this release.\n' "$target"
}
