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
  fi
  validate_uninstall_launchers
  if (( purge_config )); then
    validate_purge_ownership config "$config_root" "$config_root/.session-kit-owned.json"
  fi
  if (( purge_code )); then
    validate_purge_ownership install "$install_root" "$install_root/.session-kit-owned.json"
  fi
  validate_mutable_targets
  begin_transaction uninstall
  disable_login
  for helper in "${helpers[@]}"; do
    [[ ! -e $bin_dir/$helper || ( -f $bin_dir/$helper && ! -L $bin_dir/$helper ) ]] ||
      die "refusing to remove unsafe launcher: $bin_dir/$helper"
    command rm -f -- "$bin_dir/$helper"
  done
  command rm -f -- "$bin_dir/session-kit"
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
  printf 'Session Kit integration removed. Journals, archives, and session data were retained.\n'
  printf 'No service was stopped or restarted. Review active sessions before removing service files.\n'
}
