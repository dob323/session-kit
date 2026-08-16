#!/usr/bin/env bash
# Session Kit user service control: systemd units on Linux, LaunchAgents on
# macOS. Source this file; do not execute it.
#
# Source order: bin/session-kit sources this module after it assigns
# install_root, state_root, service_root, launchd_units, launchd_template_root,
# and launchd_receipt, and after it defines die() and platform().
# services_command() also calls validate_roots() from
# lib/sh/session_kit_checks.sh.
#
# Globals the entry script owns are assigned there, not here.
# shellcheck disable=SC2154

launchd_receipt_matches() {
  python3 - "$launchd_receipt" "$1" "$2" <<'PY'
import hashlib,json,os,pathlib,stat,sys
receipt=pathlib.Path(sys.argv[1]); unit=sys.argv[2]; active=pathlib.Path(sys.argv[3])
try:
    info=receipt.lstat()
    value=json.loads(receipt.read_text(encoding="utf-8"))
    active_info=active.lstat()
except (OSError,ValueError):
    raise SystemExit(1)
if (
    not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
    or stat.S_IMODE(info.st_mode) != 0o600
    or not stat.S_ISREG(active_info.st_mode) or active_info.st_uid != os.geteuid()
    or value.get("schema_version") != 1
    or set(value) != {"schema_version", "digests"}
    or not isinstance(value.get("digests"), dict)
):
    raise SystemExit(1)
expected=value["digests"].get(unit)
actual=hashlib.sha256(active.read_bytes()).hexdigest()
raise SystemExit(0 if isinstance(expected,str) and expected==actual else 1)
PY
}

write_launchd_receipt() {
  python3 - "$launchd_receipt" "$service_root" "${launchd_units[@]}" <<'PY'
import hashlib,json,os,pathlib,tempfile,sys
receipt=pathlib.Path(sys.argv[1]); root=pathlib.Path(sys.argv[2]); units=sys.argv[3:]
payload={
    "schema_version":1,
    "digests":{unit:hashlib.sha256((root/unit).read_bytes()).hexdigest() for unit in units},
}
receipt.parent.mkdir(parents=True,exist_ok=True)
fd,temporary=tempfile.mkstemp(prefix=f".{receipt.name}.",dir=receipt.parent)
try:
    os.fchmod(fd,0o600)
    with os.fdopen(fd,"w",encoding="utf-8") as handle:
        json.dump(payload,handle,indent=2,sort_keys=True); handle.write("\n")
        handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary,receipt)
finally:
    try: os.unlink(temporary)
    except FileNotFoundError: pass
PY
}

services_command() {
  local action=${1:-}
  [[ $# == 1 ]] || die "services requires exactly one action: enable, disable, or status"
  case "$action" in enable|disable|status) ;; *) die "services action must be enable, disable, or status" ;; esac
  local current_platform unit label domain="gui/$UID"
  current_platform=$(platform)
  if [[ $current_platform == linux ]]; then
    case "$action" in
      enable)
        systemctl --user daemon-reload
        # The watchdog is the unit that recovers a session whose terminal
        # died. Every Linux install writes its unit file, so leaving it out of
        # enable installed it dead while every file-level check still passed.
        systemctl --user enable --now shpool.socket shpool-reaper.timer \
          session-kit-subagent-sweep.timer \
          session-kit-watchdog.service
        ;;
      disable)
        systemctl --user disable --now session-kit-watchdog.service \
          shpool-reaper.timer session-kit-subagent-sweep.timer shpool.socket
        ;;
      status)
        systemctl --user --no-pager status shpool.socket shpool-reaper.timer \
          session-kit-subagent-sweep.timer \
          session-kit-watchdog.service
        ;;
    esac
    return
  fi
  [[ $current_platform == macos ]] || die "services is supported only on Linux and macOS"
  validate_roots
  for unit in "${launchd_units[@]}"; do
    [[ -f $launchd_template_root/$unit && ! -L $launchd_template_root/$unit ]] ||
      die "LaunchAgent template is missing or unsafe: $launchd_template_root/$unit"
    plutil -lint "$launchd_template_root/$unit" >/dev/null || die "invalid LaunchAgent: $unit"
  done
  service_list_json() {
    python3 "$install_root/current/lib/session_inventory.py" platform timeout 10 -- \
      shpool -D list --json
  }
  sessions_are_empty() {
    python3 -c 'import json,sys; v=json.load(sys.stdin); raise SystemExit(0 if isinstance(v,dict) and v.get("sessions")==[] else 1)'
  }
  validate_active_launchagents() {
    local active_unit
    for active_unit in "${launchd_units[@]}"; do
      if [[ -e $service_root/$active_unit || -L $service_root/$active_unit ]]; then
        [[ -f $service_root/$active_unit && ! -L $service_root/$active_unit ]] ||
          die "refusing unsafe LaunchAgent destination: $service_root/$active_unit"
        if ! cmp -s "$launchd_template_root/$active_unit" "$service_root/$active_unit" &&
            ! launchd_receipt_matches "$active_unit" "$service_root/$active_unit"; then
          die "refusing a modified or unowned LaunchAgent: $service_root/$active_unit"
        fi
      fi
    done
  }
  case "$action" in
    enable)
      launchctl print "$domain" >/dev/null 2>&1 ||
        die "the macOS GUI user domain is unavailable; log into the Mac desktop first"
      label=com.session-kit.shpool
      validate_active_launchagents
      if ! launchctl print "$domain/$label" >/dev/null 2>&1; then
        local probe_status=0
        service_list_json >/dev/null 2>&1 || probe_status=$?
        if (( probe_status == 0 )); then
          die "an unmanaged shpool daemon is already reachable; refusing a second daemon"
        elif (( probe_status == 124 )); then
          die "the shpool probe timed out; refusing a possible second daemon"
        fi
      fi
      mkdir -p "$service_root"
      for unit in "${launchd_units[@]}"; do
        label=${unit%.plist}
        if launchctl print "$domain/$label" >/dev/null 2>&1; then
          [[ -f $service_root/$unit && ! -L $service_root/$unit ]] ||
            die "loaded label has no owned LaunchAgent file: $label"
          cmp -s "$launchd_template_root/$unit" "$service_root/$unit" ||
            die "loaded label has a pending definition; disable services before enabling again: $label"
          printf 'Already loaded: %s\n' "$label"
          continue
        fi
        if [[ -e $service_root/$unit || -L $service_root/$unit ]]; then
          [[ -f $service_root/$unit && ! -L $service_root/$unit ]] ||
            die "refusing unsafe LaunchAgent destination: $service_root/$unit"
          cmp -s "$launchd_template_root/$unit" "$service_root/$unit" ||
            die "refusing to replace a different LaunchAgent: $service_root/$unit"
        else
          install -m 0644 "$launchd_template_root/$unit" "$service_root/$unit"
        fi
        if ! launchctl bootstrap "$domain" "$service_root/$unit"; then
          die "failed to load LaunchAgent: $label; previously loaded jobs were left running"
        fi
        printf 'Loaded: %s\n' "$label"
      done
      write_launchd_receipt
      ;;
    disable)
      validate_active_launchagents
      local sessions_json lock_file=$state_root/create.lock shpool_loaded=0
      launchctl print "$domain/com.session-kit.shpool" >/dev/null 2>&1 && shpool_loaded=1
      mkdir -p "$state_root"
      exec 9>"$lock_file" || die "cannot open the Session Kit action lock"
      python3 - 9 <<'PY'
import fcntl,sys
fcntl.flock(int(sys.argv[1]),fcntl.LOCK_EX)
PY
      if (( shpool_loaded )); then
        sessions_json=$(service_list_json 2>/dev/null) ||
          die "cannot prove shpool has no sessions; refusing to unload services"
        sessions_are_empty <<<"$sessions_json" || die "shpool still has sessions; refusing to unload services"
      fi
      for unit in com.session-kit.watchdog.plist com.session-kit.reaper.plist; do
        label=${unit%.plist}
        if launchctl print "$domain/$label" >/dev/null 2>&1; then
          launchctl bootout "$domain/$label"
          printf 'Unloaded: %s\n' "$label"
        else
          printf 'Already unloaded: %s\n' "$label"
        fi
        if [[ -e $service_root/$unit || -L $service_root/$unit ]]; then
          [[ -f $service_root/$unit && ! -L $service_root/$unit ]] ||
            die "refusing unsafe LaunchAgent destination: $service_root/$unit"
          cmp -s "$launchd_template_root/$unit" "$service_root/$unit" ||
            die "refusing to remove a different LaunchAgent: $service_root/$unit"
          command rm -- "$service_root/$unit"
        fi
      done
      if (( shpool_loaded )); then
        sessions_json=$(service_list_json 2>/dev/null) ||
          die "cannot re-prove empty shpool before unload; shpool was left running"
        sessions_are_empty <<<"$sessions_json" ||
          die "a session appeared during disable; shpool was left running"
      fi
      unit=com.session-kit.shpool.plist
      label=${unit%.plist}
      if launchctl print "$domain/$label" >/dev/null 2>&1; then
        launchctl bootout "$domain/$label"
        printf 'Unloaded: %s\n' "$label"
      else
        printf 'Already unloaded: %s\n' "$label"
      fi
      if [[ -e $service_root/$unit ]]; then
        command rm -- "$service_root/$unit"
      fi
      if [[ -e $launchd_receipt || -L $launchd_receipt ]]; then
        [[ -f $launchd_receipt && ! -L $launchd_receipt ]] ||
          die "refusing unsafe LaunchAgent receipt: $launchd_receipt"
        command rm -- "$launchd_receipt"
      fi
      exec 9>&-
      ;;
    status)
      local any_missing=0
      for unit in "${launchd_units[@]}"; do
        label=${unit%.plist}
        if launchctl print "$domain/$label" >/dev/null 2>&1; then
          printf 'LOADED   %s\n' "$label"
        else
          printf 'UNLOADED %s\n' "$label"
          any_missing=1
        fi
      done
      (( any_missing == 0 ))
      ;;
  esac
}
