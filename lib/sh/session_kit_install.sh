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

install_defaults() {
  local release_id=$1
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
}

provider_hooks_tool=$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)/sessionkit_supervisor/provider_hooks.py

configure_provider_hooks() {
  local release_id=$1 action=${2:-auto}
  [[ $action == auto ]] || [[ $action == enable || $action == disable ]] ||
    die "invalid provider hook action: $action"
  if [[ $action == auto ]]; then
    action=disable
    [[ -f $install_root/releases/$release_id/config/codex/hooks.json ]] && action=enable
  fi
  [[ -f $provider_hooks_tool && ! -L $provider_hooks_tool ]] ||
    die "provider hook registration tool is unavailable"
  python3 "$provider_hooks_tool" "$action" \
      --claude-settings "$claude_settings" --codex-hooks "$codex_hooks"
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
  local unit shpool_path
  for unit in "${systemd_units[@]}"; do
    install -m 0644 "$install_root/releases/$release_id/systemd/$unit" \
      "$service_root/$unit"
  done
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
  write_release_manifest "$staging" "$release_id"
  mv "$staging" "$release_dir"
  chmod 0555 "$release_dir"
  verify_release "$release_dir" "$release_id"
}

install_command() {
  local source= login=prompt journal=prompt projects=prompt answer noninteractive=0
  while (($#)); do
    case "$1" in
      --source) require_arg "$1" "${2:-}"; source=$2; shift 2 ;;
      --enable-login) login=enable; shift ;;
      --disable-login) login=disable; shift ;;
      --journal) require_arg "$1" "${2:-}"; journal=$2; shift 2 ;;
      --import-projects) projects=import; shift ;;
      --no-import-projects) projects=skip; shift ;;
      --non-interactive)
        noninteractive=1
        [[ $journal != prompt ]] || journal=off
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
    if [[ -L $install_root/current && -f $integration_marker &&
          ! -L $integration_marker ]]; then
      # A rollover over an existing installation with validated login keeps
      # it. Disabling (which also invalidates the launch marker) must be an
      # explicit --disable-login choice, never a silent non-interactive default.
      login=enable
    elif [[ -t 0 && $noninteractive == 0 ]]; then
      printf 'Enable the Session Kit picker for interactive SSH logins? [Y/n] '
      read -r answer
      case ${answer:-y} in [Nn]*) login=disable ;; *) login=enable ;; esac
    else
      login=disable
    fi
  fi
  if [[ $journal == prompt ]]; then
    if [[ -t 0 ]]; then
      printf 'Terminal journals can contain raw commands and output in ~/.local/state/shpool-journal. Opt in to keep them? [y/N] '
      read -r answer
      case ${answer:-n} in [Yy]*) journal=on ;; *) journal=off ;; esac
    else
      journal=off
    fi
  fi
  validate_mutable_targets
  if [[ -e $install_root/releases/$release_id ]]; then
    verify_release "$install_root/releases/$release_id" "$release_id"
  fi
  begin_transaction install
  install_release "$source" "$release_id"
  # Management stays on the newest successfully verified release even when
  # the user later rolls the session payload back. This recovery pointer is
  # deliberately independent of the selected runtime release.
  atomic_symlink "$install_root/releases/$release_id" "$manager_link"
  atomic_symlink "$install_root/releases/$release_id" "$install_root/current"
  lifecycle_failpoint current
  install_launchers "$release_id"
  install_completions "$release_id"
  install_defaults "$release_id"
  configure_provider_hooks "$release_id"
  lifecycle_failpoint provider-hooks
  lifecycle_failpoint themes
  configure_initial_projects "$projects" "$noninteractive"
  install_platform_services "$release_id"
  set_journal_choice "$journal"
  if [[ $login == enable ]]; then enable_login; else disable_login; fi
  write_ownership_markers
  write_receipt "$release_id" "$source" "$previous" "$current_platform"
  commit_transaction
  printf 'Installed Session Kit release %s.\n' "$release_id"
  if [[ $current_platform == linux ]]; then
    printf 'User services were installed but not started. Review them, then run:\n'
    printf '  systemctl --user daemon-reload\n'
    printf '  systemctl --user enable --now shpool.socket shpool-reaper.timer\n'
  elif [[ $current_platform == macos ]]; then
    printf 'LaunchAgents were installed but not loaded. Review them, then run:\n'
    printf '  session-kit services enable\n'
  fi
  printf 'Run session-kit doctor before opening a new managed session.\n'
}

update_command() {
  local source= login= journal=off
  while (($#)); do
    case "$1" in
      --source) require_arg "$1" "${2:-}"; source=$2; shift 2 ;;
      --enable-login) login=enable; shift ;;
      --disable-login) login=disable; shift ;;
      --journal) require_arg "$1" "${2:-}"; journal=$2; shift 2 ;;
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
  local active_tool=$install_root/current/deploy/session-kit-release
  if [[ -f $active_tool && ! -L $active_tool ]]; then
    python3 - "$active_tool" "$HOME" <<'PY'
import pathlib
import runpy
import sys

namespace = runpy.run_path(sys.argv[1], run_name="session_kit_release_gate")
namespace["validate_rollback_launch_records"](pathlib.Path(sys.argv[2]))
PY
  fi
  validate_mutable_targets
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
  atomic_symlink "$install_root/releases/$target" "$install_root/current"
  lifecycle_failpoint current
  install_codex_themes "$target"
  configure_provider_hooks "$target"
  lifecycle_failpoint provider-hooks
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
  printf 'Rolled back Session Kit to %s. Services were not restarted.\n' "$target"
}
