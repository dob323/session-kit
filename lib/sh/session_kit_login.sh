#!/usr/bin/env bash
# Session Kit shell login integration and journal opt-in.
# Source this file; do not execute it.
#
# Source order: bin/session-kit sources this module after it assigns
# bashrc_path, bash_profile_path, zshrc_path, login_start, login_end,
# state_root, install_root, and integration_marker, and after it defines die()
# and platform(). enable_login() also calls current_release_id() from
# lib/sh/session_kit_lifecycle.sh.
#
# Globals the entry script owns are assigned there, not here.
# shellcheck disable=SC2154

rewrite_login() {
  local action=$1 shell_path shell_kind
  local -a shell_specs=("$bashrc_path:bash")
  [[ $(platform) != macos ]] || shell_specs+=("$bash_profile_path:bash-login" "$zshrc_path:zsh")
  for shell_kind in "${shell_specs[@]}"; do
    shell_path=${shell_kind%:*}
    shell_kind=${shell_kind##*:}
    mkdir -p "$(dirname -- "$shell_path")"
    if [[ $action == disable && ! -e $shell_path && ! -L $shell_path ]]; then
      continue
    fi
    [[ -e $shell_path ]] || : > "$shell_path"
    [[ -f $shell_path && ! -L $shell_path ]] ||
      die "refusing to modify non-regular shell file: $shell_path"
    python3 - "$shell_path" "$action" "$login_start" "$login_end" "$shell_kind" <<'PY'
import os
import pathlib
import sys
import tempfile

path_raw, action, start, end, shell_kind = sys.argv[1:6]
path = pathlib.Path(path_raw)
text = path.read_text(encoding="utf-8")
begin = text.find(start)
finish = text.find(end)
if (begin < 0) != (finish < 0) or (begin >= 0 and finish < begin):
    raise SystemExit("session-kit: shell integration markers are incomplete; refusing to edit")
if begin >= 0:
    finish += len(end)
    if finish < len(text) and text[finish] == "\n":
        finish += 1
    before = text[:begin].rstrip("\n")
    after = text[finish:].lstrip("\n")
    text = before + ("\n" if before and after else "") + after
if action == "enable":
    if shell_kind in {"bash", "bash-login"}:
        body = (
            'if [[ -r "$HOME/.local/lib/session-kit/current/bashrc/shpool.bashrc" ]]; then\n'
            '  source "$HOME/.local/lib/session-kit/current/bashrc/shpool.bashrc"\n'
            "fi\n"
        )
    else:
        body = (
            'export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"\n'
        )
    block = f"{start}\n{body}{end}\n"
    text = text.rstrip("\n") + ("\n\n" if text.strip() else "") + block
fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
try:
    os.fchmod(fd, path.stat().st_mode & 0o777)
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
  done
}

backup_login_once() {
  local backup_root=$state_root/backups shell_path shell_name backup absent
  umask 077
  mkdir -p "$backup_root"
  chmod 700 "$backup_root" 2>/dev/null || true
  local -a shell_specs=("$bashrc_path:bashrc")
  [[ $(platform) != macos ]] || shell_specs+=("$bash_profile_path:bash-profile" "$zshrc_path:zshrc")
  for shell_name in "${shell_specs[@]}"; do
    shell_path=${shell_name%:*}
    shell_name=${shell_name##*:}
    backup=$backup_root/$shell_name.before-session-kit
    absent=$backup_root/$shell_name.was-absent
    [[ -e $backup || -e $absent ]] && continue
    if [[ -e $shell_path || -L $shell_path ]]; then
      [[ -f $shell_path && ! -L $shell_path ]] ||
        die "refusing to back up non-regular shell file: $shell_path"
      install -m 0600 "$shell_path" "$backup"
    else
      : > "$absent"
      chmod 600 "$absent"
    fi
  done
}

set_journal_choice() {
  case "$1" in
    on)
      if [[ -e $HOME/.no_shpool_journal ]]; then
        [[ -f $HOME/.no_shpool_journal && ! -L $HOME/.no_shpool_journal ]] ||
          die "refusing to replace unsafe journal sentinel"
        command rm -- "$HOME/.no_shpool_journal"
      fi
      ;;
    off)
      if [[ -e $HOME/.no_shpool_journal ]]; then
        [[ -f $HOME/.no_shpool_journal && ! -L $HOME/.no_shpool_journal ]] ||
          die "refusing to replace unsafe journal sentinel"
      else
        (umask 077; : > "$HOME/.no_shpool_journal")
      fi
      ;;
    preserve)
      # Keep whatever the operator has: sentinel present stays present,
      # absent stays absent. Rollovers and updates land here by default.
      ;;
    *) die "--journal must be on, off, or preserve" ;;
  esac
}

enable_login() {
  backup_login_once
  rewrite_login enable
  if [[ -L $install_root/current ]]; then
    local release_id
    release_id=$(current_release_id)
    [[ $release_id =~ ^[0-9a-f]{40}$ ]] || die "active release is invalid"
    umask 077
    mkdir -p "$state_root"
    printf 'session-kit-integration-v1 %s\n' "$release_id" > "$integration_marker"
    chmod 600 "$integration_marker"
  fi
  [[ ${1:-} == --quiet ]] ||
    printf 'Login integration is on. Turn it off with: session-kit disable-login\n'
}

disable_login() {
  rewrite_login disable
  if [[ -e $integration_marker ]]; then
    [[ -f $integration_marker && ! -L $integration_marker ]] ||
      die "refusing to remove unsafe integration marker"
    command rm -- "$integration_marker"
  fi
  [[ ${1:-} == --quiet ]] ||
    printf 'Login integration is off. Existing sessions and history were not changed.\n'
}
