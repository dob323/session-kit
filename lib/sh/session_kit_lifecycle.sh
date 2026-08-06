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
import os
import pathlib
import stat
import sys

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
        and stat.S_IMODE(info.st_mode) & 0o022 == 0
    ):
        raise SystemExit(f"session-kit: unsafe Codex path ancestor: {path}")
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
import json
import os
import pathlib
import stat
import sys
import tempfile
import time
import uuid

operation, journal_raw, *values = sys.argv[1:]
journal = pathlib.Path(journal_raw)

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
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass

def capture(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"path": str(path), "kind": "absent"}
    if stat.S_ISLNK(info.st_mode):
        return {"path": str(path), "kind": "symlink", "target": os.readlink(path)}
    if stat.S_ISREG(info.st_mode):
        return {
            "path": str(path),
            "kind": "file",
            "mode": stat.S_IMODE(info.st_mode),
            "content": base64.b64encode(path.read_bytes()).decode("ascii"),
        }
    raise SystemExit(f"session-kit: transaction target is unsafe: {path}")

def restore(entry):
    path = pathlib.Path(entry["path"])
    try:
        info = path.lstat()
    except FileNotFoundError:
        info = None
    if info is not None and not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
        raise SystemExit(f"session-kit: refusing incompatible transaction state: {path}")
    if info is not None:
        path.unlink()
    kind = entry["kind"]
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
    if len(paths) != len(entries) or paths[:len(values)] != values:
        raise SystemExit("session-kit: lifecycle transaction targets do not match this installation")
    theme_entries = entries[len(values):]
    expected_themes = [
        f"sk-{color}.tmTheme"
        for color in (
            "red", "blue", "green", "yellow", "purple", "orange", "pink", "cyan",
            "lime", "magenta", "silver", "sand", "sky", "sea",
        )
    ]
    if theme_entries:
        if len(theme_entries) != len(expected_themes):
            raise SystemExit("session-kit: invalid recorded theme transaction targets")
        theme_paths = [pathlib.Path(entry["path"]) for entry in theme_entries]
        parents = {path.parent for path in theme_paths}
        if (
            [path.name for path in theme_paths] != expected_themes
            or len(parents) != 1
            or next(iter(parents)).name != "themes"
        ):
            raise SystemExit("session-kit: invalid recorded theme transaction targets")
        parent = next(iter(parents))
        current = pathlib.Path(parent.anchor)
        for part in parent.relative_to(current).parts:
            current = current / part
            try:
                ancestor = current.lstat()
            except FileNotFoundError:
                raise SystemExit(f"session-kit: recorded theme ancestor is missing: {current}")
            if (
                not stat.S_ISDIR(ancestor.st_mode)
                or stat.S_ISLNK(ancestor.st_mode)
                or ancestor.st_uid not in {0, os.geteuid()}
                or stat.S_IMODE(ancestor.st_mode) & 0o022
            ):
                raise SystemExit(f"session-kit: unsafe recorded theme ancestor: {current}")
    for entry in reversed(entries):
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise SystemExit("session-kit: invalid lifecycle transaction entry")
        restore(entry)
    journal.unlink()
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
}

transaction_targets() {
  transaction_core_targets
  codex_theme_layout targets
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

lifecycle_failpoint() {
  [[ ${SESSION_KIT_TEST_FAILPOINT:-} != "$1" ]] ||
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
