#!/usr/bin/env bash
# Session Kit uninstall command. Source this file; do not execute it.
#
# Source order: bin/session-kit sources this module after it assigns the
# lifecycle roots, helpers, launchd_units, and receipt_path, and after it
# defines die() and platform(). uninstall_command() also calls the validators
# in lib/sh/session_kit_checks.sh, the transaction helpers in
# lib/sh/session_kit_lifecycle.sh, and disable_login() in
# lib/sh/session_kit_login.sh.
#
# Globals the entry script owns are assigned there, not here.
# shellcheck disable=SC2154

# Only files that still carry the kit's own marker line are removed. A copy the
# user edited, or a completion for a same-named command from somewhere else,
# survives an uninstall untouched.
remove_completions() {
  local name destination
  [[ $completion_dir != none ]] || return 0
  [[ -d $completion_dir && ! -L $completion_dir ]] || return 0
  for name in "${completion_names[@]}"; do
    destination=$completion_dir/$name
    [[ -f $destination && ! -L $destination ]] || continue
    grep -qxF -- "$completion_marker" "$destination" 2>/dev/null || continue
    command rm -f -- "$destination"
  done
  return 0
}

remove_claude_integration() {
  local action=${1:-remove} release_id claude_root accounts_root source_root
  release_id=$(current_release_id)
  source_root=$install_root/releases/$release_id/config/claude
  if [[ ! -f $source_root/nameintent_title.sh || ! -f $source_root/statusline.sh ]] &&
     [[ -L $manager_link ]]; then
    source_root=$manager_link/config/claude
  fi
  claude_root=${SESSION_KIT_CLAUDE_HOME:-$HOME/.claude}
  accounts_root=${XDG_DATA_HOME:-$HOME/.local/share}/session-kit/accounts/claude
  python3 - "$source_root" "$HOME" "$claude_root" "$accounts_root" \
    "$claude_statusline_backups" "$claude_integration_ledger" "$action" <<'PY'
import hashlib
import json
import os
import pathlib
import re
import stat
import sys
import tempfile

source_root, home, claude_root, accounts_root, backups_path, ledger_path = map(
    pathlib.Path, sys.argv[1:7]
)
action = sys.argv[7]
hook_path = claude_root / "hooks" / "nameintent_title.sh"
status_path = claude_root / "statusline.sh"
hook_command = "~/.claude/hooks/nameintent_title.sh" if claude_root == home / ".claude" else str(hook_path)
status_command = "~/.claude/statusline.sh" if claude_root == home / ".claude" else str(status_path)
canonical_status = {
    "type": "command",
    "command": status_command,
    "refreshInterval": 2,
}


def remove_exact(path, source):
    try:
        info = path.lstat()
        expected = source.read_bytes()
        payload = path.read_bytes()
    except OSError:
        return
    if (
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and info.st_uid == os.geteuid()
        and payload == expected
    ):
        path.unlink()


def load_integration_ledger(path):
    try:
        info = path.lstat()
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"session-kit: invalid Claude integration ledger: {path}: {exc}")
    files = value.get("files") if isinstance(value, dict) else None
    expected_files = {os.fspath(hook_path), os.fspath(status_path)}
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.geteuid()
        or not isinstance(value, dict)
        or value.get("schema_version") != 1
        or not isinstance(value.get("release_id"), str)
        or not re.fullmatch(r"[0-9a-f]{40}", value["release_id"])
        or not isinstance(files, dict)
        or not set(files).issubset(expected_files)
        or any(
            not isinstance(name, str)
            or not pathlib.Path(name).is_absolute()
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            for name, digest in files.items()
        )
    ):
        raise SystemExit(f"session-kit: invalid Claude integration ledger: {path}")
    return value, expected_files - set(files)


def remove_recorded(path, recorded):
    expected = recorded.get(os.fspath(path))
    if expected is None:
        return
    try:
        info = path.lstat()
        payload = path.read_bytes()
    except OSError:
        return False
    if (
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and info.st_uid == os.geteuid()
        and hashlib.sha256(payload).hexdigest() == expected
    ):
        path.unlink()
        return True
    return False


def atomic_json(path, value, mode):
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def load_backups(path):
    try:
        info = path.lstat()
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema_version": 1, "entries": {}}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"session-kit: invalid Claude status-line backup: {path}: {exc}; move that file aside to continue (its recorded status-line restores are lost to the kit)")
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.geteuid()
        or not isinstance(value, dict)
        or value.get("schema_version") != 1
        or not isinstance(value.get("entries"), dict)
    ):
        raise SystemExit(f"session-kit: invalid Claude status-line backup: {path}; move that file aside to continue (its recorded status-line restores are lost to the kit)")
    for key, record in value["entries"].items():
        if (
            not isinstance(key, str)
            or not pathlib.Path(key).is_absolute()
            or not isinstance(record, dict)
            or not isinstance(record.get("present"), bool)
            or set(record) - {"present", "value"}
            or (record["present"] and "value" not in record)
        ):
            raise SystemExit(f"session-kit: invalid Claude status-line backup: {path}; move that file aside to continue (its recorded status-line restores are lost to the kit)")
    return value


backups = load_backups(backups_path)
backup_entries = backups["entries"]
recorded_integration = load_integration_ledger(ledger_path)
if action == "preflight":
    raise SystemExit(0)
if action != "remove":
    raise SystemExit("session-kit: invalid Claude integration removal action")


def unregister(path):
    try:
        info = path.lstat()
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or not isinstance(settings, dict):
        return
    backup = backup_entries.pop(os.fspath(path), None)
    if settings.get("statusLine") == canonical_status:
        if isinstance(backup, dict) and backup.get("present") is True and "value" in backup:
            settings["statusLine"] = backup["value"]
        else:
            settings.pop("statusLine")
    hooks = settings.get("hooks")
    if isinstance(hooks, dict):
        for event in ("SessionStart", "UserPromptSubmit", "Stop"):
            groups = hooks.get(event)
            if not isinstance(groups, list):
                continue
            refreshed = []
            for group in groups:
                if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                    refreshed.append(group)
                    continue
                kept = [
                    hook for hook in group["hooks"]
                    if not (
                        isinstance(hook, dict)
                        and hook.get("type") == "command"
                        and hook.get("command") == hook_command
                    )
                ]
                if kept or set(group) != {"hooks"}:
                    copy = dict(group)
                    copy["hooks"] = kept
                    refreshed.append(copy)
            if refreshed:
                hooks[event] = refreshed
            else:
                hooks.pop(event, None)
        if not hooks:
            settings.pop("hooks", None)
    atomic_json(path, settings, stat.S_IMODE(info.st_mode))


settings_paths = [claude_root / "settings.json"]
try:
    entries = sorted(accounts_root.iterdir(), key=lambda path: path.name)
except OSError:
    entries = []
for entry in entries:
    if re.fullmatch(r"[A-Za-z0-9._-]+", entry.name) and entry.is_dir() and not entry.is_symlink():
        settings_paths.append(entry / "settings.json")
for path in settings_paths:
    unregister(path)
if backup_entries:
    atomic_json(backups_path, backups, 0o600)
else:
    try:
        info = backups_path.lstat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise SystemExit(f"session-kit: unsafe Claude status-line backup: {backups_path}")
        backups_path.unlink()
if recorded_integration is None:
    # Compatibility for installations made before the integration ledger.
    remove_exact(hook_path, source_root / "nameintent_title.sh")
    remove_exact(status_path, source_root / "statusline.sh")
else:
    ledger, missing_files = recorded_integration
    recorded_files = ledger["files"]
    retained_files = []
    for path in (hook_path, status_path):
        key = os.fspath(path)
        if key not in recorded_files:
            continue
        if remove_recorded(path, recorded_files):
            recorded_files.pop(key)
        else:
            retained_files.append(key)
    if missing_files:
        print(
            "session-kit: incomplete Claude integration ledger retained; "
            "unrecorded files were not removed: " + ", ".join(sorted(missing_files)),
            file=sys.stderr,
        )
    if retained_files:
        print(
            "session-kit: Claude integration ledger retained; "
            "recorded files were not removed: " + ", ".join(sorted(retained_files)),
            file=sys.stderr,
        )
    if missing_files or recorded_files:
        atomic_json(ledger_path, ledger, 0o600)
    else:
        ledger_path.unlink()

PY
}

remove_claude_quota_cache() {
  python3 - "$HOME" <<'PY'
import os
import pathlib
import shutil
import stat
import sys

home = pathlib.Path(sys.argv[1])
quota_root = home / ".claude" / "cache" / "session-kit-quota"
# The status line owns this whole tree. It may contain symlinks into enrolled
# account profiles, so validate the parents and remove the links without
# following them only after the uninstall transaction has committed.
for ancestor in (home / ".claude", home / ".claude" / "cache"):
    try:
        info = ancestor.lstat()
    except FileNotFoundError:
        break
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.geteuid()
    ):
        print(
            f"session-kit: quota cache retained; unsafe parent: {ancestor}",
            file=sys.stderr,
        )
        break
else:
    try:
        info = quota_root.lstat()
    except FileNotFoundError:
        pass
    else:
        if (
            stat.S_ISDIR(info.st_mode)
            and not stat.S_ISLNK(info.st_mode)
            and info.st_uid == os.geteuid()
        ):
            try:
                shutil.rmtree(quota_root)
            except OSError as exc:
                print(
                    f"session-kit: quota cache retained: {quota_root}: {exc}",
                    file=sys.stderr,
                )
        else:
            print(
                f"session-kit: quota cache retained; unsafe path: {quota_root}",
                file=sys.stderr,
            )
PY
}

uninstall_command() {
  local purge_code=0 purge_config=0 helper
  while (($#)); do
    case "$1" in
      --purge-code) purge_code=1; shift ;;
      --purge-config) purge_config=1; shift ;;
      *) die "unknown uninstall option: $1" ;;
    esac
  done
  recover_pending_transaction
  validate_roots
  if [[ $(platform) == macos ]]; then
    local unit label
    for unit in "${launchd_units[@]}"; do
      label=${unit%.plist}
      launchctl print "gui/$UID/$label" >/dev/null 2>&1 &&
        die "unload Session Kit LaunchAgents first: session-kit services disable"
      [[ ! -e $service_root/$unit && ! -L $service_root/$unit ]] ||
        die "remove inactive LaunchAgents safely with: session-kit services disable"
    done
  elif [[ $(platform) == linux ]]; then
    # macOS has refused to uninstall over live LaunchAgents since this command
    # existed; Linux never looked at its service directory at all, so an
    # uninstall left every unit file behind, pointing at code that was about
    # to be removed. Same rule, same remedy: a unit systemd still knows about
    # is stopped by the documented verb first, and the files go with the
    # uninstall rather than outliving it.
    local unit enablement activity
    for unit in "${systemd_units[@]}"; do
      # `is-enabled` answers about the link systemd holds to this file, which
      # is the answer that decides this: a unit file removed from behind a
      # live enablement is the state the old text refused to create. It has
      # more than one affirmative answer -- a unit enabled for this boot says
      # `enabled-runtime`, a hand-linked one `linked` -- and `active` is not
      # among them at all, because activity is what `is-active` reports.
      enablement=$(kit_systemctl is-enabled "$unit" 2>/dev/null || true)
      case $enablement in
        enabled | enabled-runtime | linked | linked-runtime)
          die "disable Session Kit services first: session-kit services disable"
          ;;
      esac
      # Activity is reported, never refused. A running daemon is holding the
      # operator's live sessions, no uninstall of this kit has ever ended one,
      # and removing a unit file does not stop a process that is already
      # running. Refusing here would make the only way out of an installation
      # go through killing every session in it.
      activity=$(kit_systemctl is-active "$unit" 2>/dev/null || true)
      [[ $activity != active && $activity != activating ]] ||
        printf 'session-kit: %s is still running and is left running; its unit file is removed. Stop it with: systemctl --user stop %s\n' \
          "$unit" "$unit" >&2
      # Judge every path before anything is removed. The removal below keeps
      # its own guard for the race, but a refusal discovered there has already
      # deleted the launchers and pays for it with a transaction rollback;
      # refusing here costs the operator nothing.
      [[ ! -e $service_root/$unit || ( -f $service_root/$unit && ! -L $service_root/$unit ) ]] ||
        die "refusing to remove unsafe unit file: $service_root/$unit"
    done
  fi
  validate_uninstall_launchers
  if (( purge_config )); then
    validate_purge_ownership config "$config_root" "$config_root/.session-kit-owned.json"
  fi
  if (( purge_code )); then
    validate_purge_ownership install "$install_root" "$install_root/.session-kit-owned.json"
  fi
  validate_mutable_targets
  remove_claude_integration preflight
  begin_transaction uninstall
  uninstall_transaction_pending=1
  trap 'if [[ ${uninstall_transaction_pending:-0} == 1 ]]; then recover_pending_transaction || true; fi' EXIT
  disable_login
  remove_claude_integration
  for helper in "${helpers[@]}"; do
    [[ ! -e $bin_dir/$helper || ( -f $bin_dir/$helper && ! -L $bin_dir/$helper ) ]] ||
      die "refusing to remove unsafe launcher: $bin_dir/$helper"
    command rm -f -- "$bin_dir/$helper"
  done
  command rm -f -- "$bin_dir/session-kit"
  if [[ $(platform) == linux ]]; then
    # The unit files are this installation's, and nothing else prunes them.
    for unit in "${systemd_units[@]}"; do
      [[ ! -e $service_root/$unit || ( -f $service_root/$unit && ! -L $service_root/$unit ) ]] ||
        die "refusing to remove unsafe unit file: $service_root/$unit"
      command rm -f -- "$service_root/$unit"
    done
  fi
  remove_completions
  if (( purge_config )); then
    [[ ! -e $config_root || ( -d $config_root && ! -L $config_root ) ]] ||
      die "refusing to remove unsafe config directory"
    command rm -rf -- "$config_root"
  fi
  if (( purge_code )); then
    [[ ! -e $install_root || ( -d $install_root && ! -L $install_root ) ]] ||
      die "refusing to remove unsafe install directory"
    find "$install_root" -type d -exec chmod u+rwx {} +
    command rm -rf -- "$install_root"
  fi
  command rm -f -- "$receipt_path"
  commit_transaction
  uninstall_transaction_pending=0
  trap - EXIT
  remove_claude_quota_cache
  printf 'Session Kit integration removed. Journals, archives, and session data were retained.\n'
  printf 'No service was stopped or restarted. Review active sessions before removing service files.\n'
}
