#!/usr/bin/env bash
# Session Kit lifecycle transactions, receipts, and Codex theme layout.
# Source this file; do not execute it.
#
# Source order: bin/session-kit sources this module after it assigns the
# lifecycle roots, integration_marker, receipt_path, shpool_config, helpers,
# systemd_units, launchd_units, launchd_template_root, transaction_path, and
# codex_theme_names, and after it defines die() and platform(). The functions
# here also call validate_roots() from lib/sh/session_kit_checks.sh.
#
# Globals the entry script owns are assigned there, not here.
# shellcheck disable=SC2154

# A stable launcher from a pre-hook release can source this module after an
# interrupted pointer flip. Supply the two newer globals here so that recovery
# remains usable even though that older entry script never assigned them.
claude_settings=${claude_settings:-${SESSION_KIT_CLAUDE_SETTINGS:-${SESSION_KIT_CLAUDE_HOME:-"$HOME/.claude"}/settings.json}}
codex_hooks=${codex_hooks:-${SESSION_KIT_CODEX_HOOKS:-${SESSION_KIT_CODEX_HOME:-${CODEX_HOME:-"$HOME/.codex"}}/hooks.json}}
claude_statusline_backups=${claude_statusline_backups:-${SESSION_KIT_STATE_DIR:-${XDG_STATE_HOME:-"$HOME/.local/state"}/session-kit}/claude-statusline-backups.json}
claude_integration_ledger=${claude_integration_ledger:-${SESSION_KIT_STATE_DIR:-${XDG_STATE_HOME:-"$HOME/.local/state"}/session-kit}/claude-integration.json}

atomic_symlink() {
  local target=$1 destination=$2
  python3 - "$target" "$destination" <<'PY'
import os
import pathlib
import sys
import uuid

target, destination = sys.argv[1:3]
path = pathlib.Path(destination)
path.parent.mkdir(parents=True, exist_ok=True)
temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
try:
    os.symlink(target, temporary)
    os.replace(temporary, path)
finally:
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
PY
}

codex_theme_layout() {
  local action=$1 custom=0
  if [[ -n ${SESSION_KIT_CODEX_HOME:-} || -n ${CODEX_HOME:-} ]]; then
    custom=1
  fi
  python3 - "$action" "$HOME" \
    "${SESSION_KIT_CODEX_HOME:-${CODEX_HOME:-$HOME/.codex}}" \
    "$custom" "${codex_theme_names[@]}" <<'PY'
import grp
import os
import pathlib
import pwd
import stat
import sys


# One rule, applied wherever this file decides whether a provider directory may
# be written through: no other account may be
# able to write it. A distribution that gives every account a private group and
# a 002 umask leaves a provider's own config directory group-writable, which
# exposes it to nobody, so that one shape is allowed. Every condition must hold
# and any lookup failure refuses.
def private_group(gid):
    try:
        account = pwd.getpwuid(os.geteuid())
        group = grp.getgrgid(gid)
        accounts = pwd.getpwall()
    except (KeyError, OSError):
        return False
    if gid != account.pw_gid or group.gr_name != account.pw_name or list(group.gr_mem):
        return False
    covered = False
    for other in accounts:
        if other.pw_name == account.pw_name:
            covered = other.pw_gid == gid
        elif other.pw_gid == gid:
            return False
    return covered


def mode_permits(info):
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o002:
        return False
    if not mode & 0o020:
        return True
    return info.st_uid == os.geteuid() and private_group(info.st_gid)


def write_repair(path, info):
    """Name the chmod that repairs a directory the caller owns."""
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and mode & 0o022:
        return f"; run: chmod {'go-w' if mode & 0o002 else 'g-w'} {path}"
    return ""


action, home_raw, root_raw, custom, *colors = sys.argv[1:]
home = pathlib.Path(home_raw)
root = pathlib.Path(root_raw)
default_root = home / ".codex"

if not root.is_absolute() or ".." in root.parts:
    raise SystemExit("session-kit: Codex home must be an absolute normalized path")


def directory_info(path: pathlib.Path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not (
        stat.S_ISDIR(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and info.st_uid in {0, os.geteuid()}
        and mode_permits(info)
    ):
        raise SystemExit(
            f"session-kit: unsafe Codex path ancestor: {path}{write_repair(path, info)}"
        )
    return info


def validate_chain(path: pathlib.Path) -> None:
    try:
        relative = path.relative_to(home)
    except ValueError:
        base = pathlib.Path(path.anchor)
        relative = path.relative_to(base)
    else:
        base_info = directory_info(home)
        if base_info is None or base_info.st_uid != os.geteuid():
            raise SystemExit("session-kit: HOME is not an owner-controlled directory")
        base = home
    if base == pathlib.Path(path.anchor) and directory_info(base) is None:
        raise SystemExit("session-kit: Codex path root is unavailable")
    current = base
    for part in relative.parts:
        current = current / part
        if directory_info(current) is None:
            break


validate_chain(root)
root_info = directory_info(root)
if root_info is None:
    if custom == "1" or root != default_root:
        raise SystemExit(
            "session-kit: custom Codex home must already be an owner-controlled directory"
        )
    if directory_info(root.parent) is None:
        raise SystemExit("session-kit: Codex home parent is unsafe")
    if action == "create":
        root.mkdir(mode=0o700)

if directory_info(root) is not None and root.lstat().st_uid != os.geteuid():
    raise SystemExit("session-kit: Codex home must be owned by the current user")

themes = root / "themes"
validate_chain(themes)
themes_info = directory_info(themes)
if themes_info is None:
    if action == "create":
        themes.mkdir(mode=0o700)
elif themes_info.st_uid != os.geteuid():
    raise SystemExit("session-kit: Codex themes must be owned by the current user")

if action == "create":
    if directory_info(themes) is None:
        raise SystemExit("session-kit: Codex themes directory could not be secured")
    print(themes)
elif action == "validate":
    pass
elif action == "targets":
    for color in colors:
        print(themes / f"sk-{color}.tmTheme")
elif action == "root":
    print(root)
else:
    raise SystemExit("session-kit: invalid Codex theme layout action")
PY
}

lifecycle_transaction() {
  python3 - "$@" <<'PY'
import base64
import grp
import json
import os
import pathlib
import pwd
import re
import stat
import sys
import tempfile
import time
import uuid


# One rule, applied wherever this file decides whether a provider directory may
# be written through: no other account may be
# able to write it. A distribution that gives every account a private group and
# a 002 umask leaves a provider's own config directory group-writable, which
# exposes it to nobody, so that one shape is allowed. Every condition must hold
# and any lookup failure refuses.
def private_group(gid):
    try:
        account = pwd.getpwuid(os.geteuid())
        group = grp.getgrgid(gid)
        accounts = pwd.getpwall()
    except (KeyError, OSError):
        return False
    if gid != account.pw_gid or group.gr_name != account.pw_name or list(group.gr_mem):
        return False
    covered = False
    for other in accounts:
        if other.pw_name == account.pw_name:
            covered = other.pw_gid == gid
        elif other.pw_gid == gid:
            return False
    return covered


def mode_permits(info):
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o002:
        return False
    if not mode & 0o020:
        return True
    return info.st_uid == os.geteuid() and private_group(info.st_gid)


def write_repair(path, info):
    """Name the chmod that repairs a directory the caller owns."""
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and mode & 0o022:
        return f"; run: chmod {'go-w' if mode & 0o002 else 'g-w'} {path}"
    return ""


operation, journal_raw, *values = sys.argv[1:]
journal = pathlib.Path(journal_raw)

def fsync_parent(path):
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

def atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_parent(path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass

def is_claude_settings(path):
    home = pathlib.Path.home()
    claude_root = pathlib.Path(
        os.environ.get("SESSION_KIT_CLAUDE_HOME", home / ".claude")
    )
    default_settings = pathlib.Path(
        os.environ.get(
            "SESSION_KIT_CLAUDE_SETTINGS", claude_root / "settings.json"
        )
    )
    if path == default_settings:
        return True
    accounts_root = pathlib.Path(
        os.environ.get("XDG_DATA_HOME", home / ".local/share")
    ) / "session-kit" / "accounts" / "claude"
    try:
        relative = path.relative_to(accounts_root)
    except ValueError:
        return False
    return bool(
        len(relative.parts) == 2
        and relative.parts[1] == "settings.json"
        and re.fullmatch(r"[A-Za-z0-9._-]+", relative.parts[0])
    )

def capture(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"path": str(path), "kind": "absent"}
    if stat.S_ISLNK(info.st_mode):
        return {"path": str(path), "kind": "symlink", "target": os.readlink(path)}
    if stat.S_ISREG(info.st_mode):
        # Claude settings owned by another account are deliberately skipped by
        # the integration writer. Do not read or later replace them merely to
        # journal a transaction that cannot change them.
        if info.st_uid != os.geteuid() and is_claude_settings(path):
            return {"path": str(path), "kind": "untouched"}
        return {
            "path": str(path),
            "kind": "file",
            "mode": stat.S_IMODE(info.st_mode),
            "content": base64.b64encode(path.read_bytes()).decode("ascii"),
        }
    raise SystemExit(f"session-kit: transaction target is unsafe: {path}")

def restore(entry):
    path = pathlib.Path(entry["path"])
    kind = entry["kind"]
    if kind == "untouched":
        return
    try:
        info = path.lstat()
    except FileNotFoundError:
        info = None
    if info is not None and not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
        raise SystemExit(f"session-kit: refusing incompatible transaction state: {path}")
    if info is not None:
        path.unlink()
    if kind == "absent":
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "symlink":
        os.symlink(entry["target"], path)
    elif kind == "file":
        data = base64.b64decode(entry["content"], validate=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            os.fchmod(fd, int(entry["mode"]))
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    else:
        raise SystemExit("session-kit: invalid lifecycle transaction entry")

if operation == "begin":
    if journal.exists() or journal.is_symlink():
        raise SystemExit(f"session-kit: pending lifecycle transaction: {journal}")
    entries = [capture(pathlib.Path(value)) for value in values[1:]]
    payload = {
        "schema_version": 1,
        "transaction_id": uuid.uuid4().hex,
        "action": values[0],
        "started_at_unix": int(time.time()),
        "state": "pending",
        "entries": entries,
    }
    atomic_json(journal, payload)
elif operation == "recover":
    if not journal.exists() and not journal.is_symlink():
        raise SystemExit(0)
    info = journal.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise SystemExit(f"session-kit: unsafe lifecycle transaction journal: {journal}")
    try:
        payload = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"session-kit: invalid lifecycle transaction journal: {exc}")
    if payload.get("schema_version") != 1 or payload.get("state") != "pending":
        raise SystemExit("session-kit: incompatible lifecycle transaction state")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise SystemExit("session-kit: invalid lifecycle transaction entries")
    paths = [entry.get("path") for entry in entries if isinstance(entry, dict)]
    base_count = len(values) - 2
    if len(paths) != len(entries) or paths[:base_count] != values[:base_count]:
        raise SystemExit("session-kit: lifecycle transaction targets do not match this installation")

    def validate_recorded_config(path_raw, expected_name):
        path = pathlib.Path(path_raw)
        if not path.is_absolute() or path.name != expected_name:
            raise SystemExit("session-kit: invalid recorded provider hook target")
        home = pathlib.Path.home()
        try:
            relative = path.parent.relative_to(home)
        except ValueError:
            current = pathlib.Path(path.anchor)
            relative = path.parent.relative_to(current)
        else:
            current = home
        for part in (".", *relative.parts):
            if part != ".":
                current = current / part
            try:
                ancestor = current.lstat()
            except FileNotFoundError:
                break
            if (
                not stat.S_ISDIR(ancestor.st_mode)
                or stat.S_ISLNK(ancestor.st_mode)
                or ancestor.st_uid not in {0, os.geteuid()}
                or not mode_permits(ancestor)
            ):
                raise SystemExit(
                    "session-kit: unsafe recorded provider hook ancestor: "
                    f"{current}{write_repair(current, ancestor)}"
                )

    core_count = base_count
    if len(paths) >= base_count + 2:
        possible_settings, possible_hooks = paths[base_count : base_count + 2]
        if (
            isinstance(possible_settings, str)
            and isinstance(possible_hooks, str)
            and pathlib.Path(possible_settings).name == "settings.json"
            and pathlib.Path(possible_hooks).name == "hooks.json"
        ):
            validate_recorded_config(possible_settings, "settings.json")
            validate_recorded_config(possible_hooks, "hooks.json")
            core_count += 2
    # Releases after provider-hook activation also transaction the two
    # kit-owned Claude files and every enrolled Claude profile's settings.
    # They are appended after themes so an older pending journal retains the
    # exact prefix the recovery code above already understands.
    trailing_entries = entries[core_count:]
    claude_root = pathlib.Path(
        os.environ.get("SESSION_KIT_CLAUDE_HOME", pathlib.Path.home() / ".claude")
    )
    accounts_root = pathlib.Path(
        os.environ.get("XDG_DATA_HOME", pathlib.Path.home() / ".local/share")
    ) / "session-kit" / "accounts" / "claude"

    def recorded_claude_integration(path_raw):
        path = pathlib.Path(path_raw)
        state_root = pathlib.Path(os.environ.get(
            "SESSION_KIT_STATE_DIR",
            pathlib.Path(os.environ.get("XDG_STATE_HOME", pathlib.Path.home() / ".local/state")) / "session-kit",
        ))
        if path in {
            state_root / "claude-statusline-backups.json",
            state_root / "claude-integration.json",
        }:
            validate_recorded_config(path_raw, path.name)
            return True
        if path in {
            claude_root / "hooks" / "nameintent_title.sh",
            claude_root / "statusline.sh",
        }:
            validate_recorded_config(path_raw, path.name)
            return True
        try:
            relative = path.relative_to(accounts_root)
        except ValueError:
            return False
        if (
            len(relative.parts) == 2
            and relative.parts[1] == "settings.json"
            and re.fullmatch(r"[A-Za-z0-9._-]+", relative.parts[0])
        ):
            validate_recorded_config(path_raw, "settings.json")
            return True
        return False

    seen_integration = set()
    while trailing_entries:
        candidate = trailing_entries[-1]
        candidate_path = candidate.get("path") if isinstance(candidate, dict) else None
        if not isinstance(candidate_path, str) or not recorded_claude_integration(candidate_path):
            break
        if candidate_path in seen_integration:
            raise SystemExit("session-kit: duplicate Claude integration transaction target")
        seen_integration.add(candidate_path)
        trailing_entries.pop()
    theme_entries = trailing_entries
    expected_themes = [
        f"sk-{color}.tmTheme"
        for color in (
            "red", "blue", "green", "yellow", "purple", "orange", "pink", "cyan",
            "lime", "magenta", "silver", "sand", "sky", "sea",
        )
    ]
    if theme_entries:
        theme_paths = [pathlib.Path(entry["path"]) for entry in theme_entries]
        recorded_names = [path.name for path in theme_paths]
        # A transaction records the themes the release it ran for actually
        # shipped, and this list is the union across releases. Requiring the
        # whole list would refuse to recover a transaction written by a release
        # with a smaller palette, so require the recorded names to appear in
        # canonical order without gaps in meaning -- a subsequence -- and to be
        # distinct. An unknown or reordered name still fails.
        remaining = list(expected_themes)
        ordered = True
        for name in recorded_names:
            while remaining and remaining[0] != name:
                remaining.pop(0)
            if not remaining:
                ordered = False
                break
            remaining.pop(0)
        parents = {path.parent for path in theme_paths}
        if (
            not ordered
            or not recorded_names
            or len(set(recorded_names)) != len(recorded_names)
            or len(parents) != 1
            or next(iter(parents)).name != "themes"
        ):
            raise SystemExit("session-kit: invalid recorded theme transaction targets")
        parent = next(iter(parents))
        home = pathlib.Path.home()
        try:
            relative = parent.relative_to(home)
        except ValueError:
            current = pathlib.Path(parent.anchor)
            relative = parent.relative_to(current)
        else:
            current = home
        for part in (".", *relative.parts):
            if part != ".":
                current = current / part
            try:
                ancestor = current.lstat()
            except FileNotFoundError:
                raise SystemExit(f"session-kit: recorded theme ancestor is missing: {current}")
            if (
                not stat.S_ISDIR(ancestor.st_mode)
                or stat.S_ISLNK(ancestor.st_mode)
                or ancestor.st_uid not in {0, os.geteuid()}
                or not mode_permits(ancestor)
            ):
                raise SystemExit(
                    "session-kit: unsafe recorded theme ancestor: "
                    f"{current}{write_repair(current, ancestor)}"
                )
    for entry in reversed(entries):
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise SystemExit("session-kit: invalid lifecycle transaction entry")
        restore(entry)
    journal.unlink()
    fsync_parent(journal)
    print(f"Recovered interrupted Session Kit {payload.get('action', 'lifecycle')} transaction.")
elif operation == "commit":
    if not journal.is_file() or journal.is_symlink():
        raise SystemExit(f"session-kit: missing lifecycle transaction journal: {journal}")
    payload = json.loads(journal.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or payload.get("state") != "pending":
        raise SystemExit("session-kit: incompatible lifecycle transaction state")
    payload["state"] = "committed"
    payload["committed_at_unix"] = int(time.time())
    backup_root = journal.parent / "backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_root, 0o700)
    backup = backup_root / (
        f"lifecycle-{payload['committed_at_unix']}-{payload['transaction_id']}.json"
    )
    atomic_json(backup, payload)
    journal.unlink()
    fsync_parent(journal)
else:
    raise SystemExit("session-kit: invalid lifecycle transaction operation")
PY
}

recover_pending_transaction() {
  validate_roots
  local -a targets=()
  mapfile -t targets < <(transaction_core_targets)
  lifecycle_transaction recover "$transaction_path" "${targets[@]}"
}

transaction_core_targets() {
  local unit helper
  printf '%s\n' \
    "$install_root/current" \
    "$bin_dir/session-kit" \
    "$bashrc_path" \
    "$bash_profile_path" \
    "$zshrc_path" \
    "$integration_marker" \
    "$HOME/.no_shpool_journal" \
    "$receipt_path" \
    "$install_root/.session-kit-owned.json" \
    "$config_root/.session-kit-owned.json"
  printf '%s\n' \
    "$config_root/projects.tsv" \
    "$config_root/inventory.json" \
    "$shpool_config"
  for helper in "${helpers[@]}"; do printf '%s\n' "$bin_dir/$helper"; done
  if [[ $(platform) == macos ]]; then
    for unit in "${launchd_units[@]}"; do printf '%s\n' "$launchd_template_root/$unit"; done
  else
    for unit in "${systemd_units[@]}"; do printf '%s\n' "$service_root/$unit"; done
  fi
  # New transaction targets are appended so a release predating provider-hook
  # activation still writes a recoverable prefix if it flips to this release.
  printf '%s\n' "$claude_settings" "$codex_hooks"
}

transaction_targets() {
  transaction_core_targets
  codex_theme_layout targets
  claude_integration_targets
}

# Claude reads these files outside the kit's release and config roots. Record
# every one before an install changes it, including the settings of already
# enrolled account profiles. The default settings file is already a legacy
# core target, so it is deliberately not repeated here.
claude_integration_targets() {
  local claude_root accounts_root account alias
  claude_root=${SESSION_KIT_CLAUDE_HOME:-$HOME/.claude}
  printf '%s\n' \
    "$claude_root/hooks/nameintent_title.sh" \
    "$claude_root/statusline.sh"
  accounts_root=${XDG_DATA_HOME:-$HOME/.local/share}/session-kit/accounts/claude
  if [[ -d $accounts_root && ! -L $accounts_root ]]; then
    for account in "$accounts_root"/*; do
      [[ -d $account && ! -L $account ]] || continue
      alias=${account##*/}
      [[ $alias =~ ^[A-Za-z0-9._-]+$ ]] || continue
      printf '%s\n' "$account/settings.json"
    done
  fi
  printf '%s\n' "$claude_statusline_backups" "$claude_integration_ledger"
}

begin_transaction() {
  local action=$1
  shift
  umask 077
  mkdir -p "$state_root"
  chmod 700 "$state_root" 2>/dev/null || true
  codex_theme_layout validate
  local -a targets=()
  mapfile -t targets < <(transaction_targets)
  lifecycle_transaction begin "$transaction_path" "$action" "${targets[@]}" "$@"
}

commit_transaction() {
  lifecycle_transaction commit "$transaction_path"
}

# True only when the isolated test harness armed exactly this failpoint.
#
# A failpoint aborts an install or update at a chosen instruction so a test can
# prove the transaction recovers. Production must never honour one: without the
# SESSION_KIT_TESTING gate, any process able to set an environment variable
# could abort every install mid-transaction just by naming a failpoint. Both
# conditions are checked in this one place so no caller can arm a failpoint by
# checking only the name.
lifecycle_failpoint_armed() {
  [[ ${SESSION_KIT_TESTING:-0} == 1 && ${SESSION_KIT_TEST_FAILPOINT:-} == "$1" ]]
}

lifecycle_failpoint() {
  lifecycle_failpoint_armed "$1" || return 0
  if [[ ${SESSION_KIT_TEST_FAILPOINT_MODE:-} == kill ]]; then
    kill -KILL "$$"
  fi
  die "isolated test failpoint after $1"
}

write_receipt() {
  local release_id=$1 source=$2 previous=$3 current_platform=$4
  umask 077
  mkdir -p "$state_root"
  chmod 700 "$state_root" 2>/dev/null || true
  python3 - "$receipt_path" "$release_id" "$source" "$previous" "$current_platform" <<'PY'
import json
import os
import pathlib
import sys
import tempfile
import time

path, release_id, source, previous, platform = sys.argv[1:6]
destination = pathlib.Path(path)
payload = {
    "schema_version": 2,
    "installed_release": release_id,
    "previous_release": previous or None,
    "source": source,
    "platform": platform,
    "installed_at_unix": int(time.time()),
    "roots": {
        "install": os.environ.get("SESSION_KIT_ROOT", str(pathlib.Path.home() / ".local/lib/session-kit")),
        "config": os.environ.get(
            "SESSION_KIT_CONFIG_ROOT",
            str(pathlib.Path(os.environ.get("XDG_CONFIG_HOME", pathlib.Path.home() / ".config")) / "session-kit"),
        ),
    },
}
fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)
finally:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
PY
}

write_ownership_markers() {
  local root marker kind
  for kind in install config; do
    if [[ $kind == install ]]; then root=$install_root; else root=$config_root; fi
    marker=$root/.session-kit-owned.json
    umask 077
    mkdir -p "$root"
    chmod 700 "$root" 2>/dev/null || true
    python3 - "$marker" "$root" "$kind" <<'PY'
import json
import os
import pathlib
import tempfile
import sys

marker, root, kind = map(pathlib.Path, sys.argv[1:4])
payload = {
    "schema_version": 1,
    "owner": "session-kit",
    "kind": str(kind),
    "root": str(root.resolve(strict=False)),
}
fd, temporary = tempfile.mkstemp(prefix=f".{marker.name}.", dir=marker.parent)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, marker)
finally:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
PY
  done
}

current_release_id() {
  local resolved
  [[ -L $install_root/current ]] || return 0
  resolved=$(cd -P -- "$install_root/current" 2>/dev/null && pwd) || return 0
  case "$resolved" in
    "$install_root"/releases/*) printf '%s\n' "${resolved##*/}" ;;
  esac
}
