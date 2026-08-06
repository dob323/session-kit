#!/usr/bin/env bash
# sp command implementations the dispatcher in bin/sp calls by name: go,
# takeover, close, prune, history, color, name, find, health, recover, and
# verify-start. Source this file; do not execute it.
#
# Source order: bin/sp sources this module ahead of its first trap, after
# SCRIPT_DIR, session_kit_common, and INVENTORY_CORE are in place. These
# functions call the snapshot, attach, and target helpers in lib/sh/sp_core.sh,
# the session builders in lib/sh/sp_sessions.sh, and reach the reaper through
# $SCRIPT_DIR.
#
# Globals the entry script owns are assigned there, not here.
# shellcheck disable=SC2154

go_target() {
  local selector=$1
  resolve_target "$selector" || return 1
  sk_require_mutation_target || return 1
  local id=$SK_ID started=$SK_STARTED provider=$SK_PROVIDER uuid=$SK_UUID cwd=$SK_CWD
  local shell_pid=$SK_SHELL_PID shell_start=$SK_SHELL_START
  local provider_pid=$SK_PROVIDER_PID provider_start=$SK_PROVIDER_START
  local daemon_pid=$SK_DAEMON_PID daemon_start=$SK_DAEMON_START
  if [[ $SK_STATUS != disconnected && $SK_STATUS != Disconnected ]]; then
    sk_die "$id is attached; use 'sp takeover $id' for a confirmed takeover"
    return 1
  fi
  sk_revalidate "$id" "$started" "$provider" "$uuid" "$shell_pid" "$shell_start" "$provider_pid" "$provider_start" "$daemon_pid" "$daemon_start" || {
    sk_die "target changed before attach; no action taken"
    return 1
  }
  attach_id "$id" "$cwd"
}

takeover_target() {
  local selector=$1
  resolve_target "$selector" || return 1
  sk_require_mutation_target || return 1
  local id=$SK_ID started=$SK_STARTED provider=$SK_PROVIDER uuid=$SK_UUID title=$SK_TITLE cwd=$SK_CWD
  local shell_pid=$SK_SHELL_PID shell_start=$SK_SHELL_START
  local provider_pid=$SK_PROVIDER_PID provider_start=$SK_PROVIDER_START
  local daemon_pid=$SK_DAEMON_PID daemon_start=$SK_DAEMON_START
  if [[ $SK_STATUS == disconnected || $SK_STATUS == Disconnected ]]; then
    sk_die "$id is ready, not attached; use 'sp go $id'"
    return 1
  fi
  sk_confirm_exact "Take over attached session" "$id" "$title" "$provider" || {
    echo "Takeover cancelled."
    return 1
  }
  sk_revalidate "$id" "$started" "$provider" "$uuid" "$shell_pid" "$shell_start" "$provider_pid" "$provider_start" "$daemon_pid" "$daemon_start" || {
    sk_die "target changed while confirming; no action taken"
    return 1
  }
  attach_id "$id" "$cwd" 1
}

close_target() {
  local selector=$1
  resolve_target "$selector" || return 1
  sk_require_mutation_target || return 1
  local id=$SK_ID started=$SK_STARTED provider=$SK_PROVIDER uuid=$SK_UUID title=$SK_TITLE
  local shell_pid=$SK_SHELL_PID shell_start=$SK_SHELL_START
  local provider_pid=$SK_PROVIDER_PID provider_start=$SK_PROVIDER_START
  local daemon_pid=$SK_DAEMON_PID daemon_start=$SK_DAEMON_START
  sk_confirm_exact "Close session" "$id" "$title" "$provider" || {
    echo "Close cancelled."
    return 1
  }
  sk_revalidate "$id" "$started" "$provider" "$uuid" "$shell_pid" "$shell_start" "$provider_pid" "$provider_start" "$daemon_pid" "$daemon_start" || {
    sk_die "target changed while confirming; no action taken"
    return 1
  }
  sk_kill_status=0
  sk_timeout 20 "$SK_SHPOOL" kill "$id" || sk_kill_status=$?
  if (( sk_kill_status == 124 || sk_kill_status == 137 )); then
    sk_die "the close did not confirm within 20s — the session may or may not have ended; refresh the list before acting again"
    sk_lock_release 9 2>/dev/null
    return 1
  fi
  if (( sk_kill_status != 0 )); then
    sk_die "shpool refused to close the exact session"
    return 1
  fi
  # Same guarantee as the picker path: nothing pinned to this session's
  # exact identity may outlive the close.
  sk_reap_session_leftovers \
    "${provider_pid:-}" "${provider_start:-}" \
    "${shell_pid:-}" "${shell_start:-}"
  if [[ -e $SK_START_DIR/$id || -L $SK_START_DIR/$id ||
        -e $SK_START_DIR/$id.expected || -L $SK_START_DIR/$id.expected ]]; then
    if ! sk_quarantine_start_record "$id" closed >/dev/null; then
      sk_die "session $id was closed, but its retained launch record could not be archived"
      return 1
    fi
  fi
  printf 'Closed exact session %s\n' "$id"
}

show_prune() {
  local candidate_file="$SK_STATE_DIR/prune-candidates.json"
  "$SCRIPT_DIR/shpool_reaper" --candidates >/dev/null || return 1
  if [[ ! -s $candidate_file ]]; then
    echo "No verified seven-day empty-shell prune candidates."
    return 0
  fi
  local count
  count=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("candidates",[])))' "$candidate_file")
  if (( count == 0 )); then
    echo "No verified seven-day empty-shell prune candidates."
    return 0
  fi
  printf 'Verified prune candidates:\n'
  if ! python3 - "$candidate_file" <<'PY'
import json,re,sys
rows=json.load(open(sys.argv[1])).get("candidates",[])
pattern=re.compile(r"(?:main(?:[1-9][0-9]*)?|s[0-9]{8}-[0-9]{6}-[1-9][0-9]*(?:-[1-9][0-9]*)?)")
for row in rows:
    value=row.get("shpool_id") if isinstance(row,dict) else None
    if not isinstance(value,str) or len(value.encode())>128 or pattern.fullmatch(value) is None:
        raise SystemExit(2)
for row in rows:
    print(f"  {row['shpool_id']}")
PY
  then
    sk_die "candidate list contains an unmanaged session ID; nothing was displayed or pruned"
    return 1
  fi
  local index private_candidate
  for (( index=0; index<count; index++ )); do
    private_candidate=$(mktemp "$SK_STATE_DIR/prune-candidate.json.XXXXXX") || return 1
    if ! python3 - "$candidate_file" "$index" > "$private_candidate" <<'PY'
import json,re,sys
rows=json.load(open(sys.argv[1])).get("candidates",[])
row=rows[int(sys.argv[2])]
required=("shpool_id","started_at_unix_ms","last_disconnected_at_unix_ms",
          "shell_pid","shell_start_ticks")
if not isinstance(row,dict) or any(key not in row for key in required):
    raise SystemExit(2)
session_id=row["shpool_id"]
if not isinstance(session_id,str) or len(session_id.encode())>128:
    raise SystemExit(2)
if not re.fullmatch(r"(?:main(?:[1-9][0-9]*)?|s[0-9]{8}-[0-9]{6}-[1-9][0-9]*(?:-[1-9][0-9]*)?)",session_id):
    raise SystemExit(2)
if not all(isinstance(row[key],int) for key in required[1:]):
    raise SystemExit(2)
json.dump({key:row[key] for key in required},sys.stdout,indent=2,sort_keys=True)
sys.stdout.write("\n")
PY
    then
      command rm -- "$private_candidate"
      sk_die "invalid or unmanaged prune candidate; no action taken"
      return 1
    fi
    chmod 600 "$private_candidate" 2>/dev/null || true
    if ! prune_target "$private_candidate"; then
      command rm -- "$private_candidate"
      return 1
    fi
    command rm -- "$private_candidate"
  done
}

prune_target() {
  local candidate_file=$1 id
  id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["shpool_id"])' "$candidate_file") || return 1
  make_guard_snapshot || return 1
  sk_resolve "$SNAPSHOT" "$id" || {
    sk_die "prune candidate no longer exists: $id"
    return 1
  }
  sk_require_mutation_target || return 1
  local title=$SK_TITLE provider=$SK_PROVIDER
  sk_confirm_exact "Prune verified empty session" "$id" "$title" "$provider" || {
    echo "Prune cancelled."
    return 1
  }

  exec 9>"$SK_STATE_DIR/create.lock" || return 1
  sk_lock_acquire 9 "$SK_STATE_DIR/create.lock" || { exec 9>&-; return 1; }
  # This is the final command before kill. It proves exact raw shpool
  # generation, disconnected status, stable shell PID/start, and an empty
  # process tree using two fresh shpool snapshots. Any ambiguity fails closed.
  "$SCRIPT_DIR/shpool_reaper" --verify-candidate "$candidate_file" >/dev/null || {
    sk_lock_release 9
    sk_die "prune candidate changed or is no longer empty; no action taken"
    return 1
  }
  sk_kill_status=0
  sk_timeout 20 "$SK_SHPOOL" kill "$id" || sk_kill_status=$?
  if (( sk_kill_status == 124 || sk_kill_status == 137 )); then
    sk_die "the close did not confirm within 20s — the session may or may not have ended; refresh the list before acting again"
    sk_lock_release 9 2>/dev/null
    return 1
  fi
  if (( sk_kill_status != 0 )); then
    sk_lock_release 9
    sk_die "shpool refused to kill exact verified candidate: $id"
    return 1
  fi
  sk_lock_release 9
  printf 'Pruned exact verified-empty session %s\n' "$id"
}

show_history() {
  local selector=$1
  resolve_target "$selector" || return 1
  show_history_id "$SK_ID"
}

show_history_id() {
  local id=$1
  local -a files=()
  while IFS= read -r path; do [[ -n $path ]] && files+=("$path"); done < <(history_files "$id")
  (( ${#files[@]} > 0 )) || {
    sk_die "no live history for $id"
    return 1
  }
  if [[ ${SESSION_KIT_NONINTERACTIVE:-0} == 1 ]]; then
    command cat -- "${files[@]}"
  else
    command cat -- "${files[@]}" | less -R
  fi
}

color_target() {
  local selector=$1 action=$2 chosen=${3:-}
  resolve_target "$selector" || return 1
  sk_require_mutation_target || return 1
  [[ $SK_PROVIDER == claude || $SK_PROVIDER == codex ]] || {
    sk_die "managed shell rows keep the derived look and cannot be colored"
    return 1
  }
  local id=$SK_ID started=$SK_STARTED provider=$SK_PROVIDER uuid=$SK_UUID
  local shell_pid=$SK_SHELL_PID shell_start=$SK_SHELL_START
  local provider_pid=$SK_PROVIDER_PID provider_start=$SK_PROVIDER_START
  local daemon_pid=$SK_DAEMON_PID daemon_start=$SK_DAEMON_START
  picker_lock || return 1
  sk_revalidate "$id" "$started" "$provider" "$uuid" \
    "$shell_pid" "$shell_start" "$provider_pid" "$provider_start" \
    "$daemon_pid" "$daemon_start" || {
      sk_lock_release 9
      sk_die "target changed before color update; nothing changed"
      return 1
    }
  if [[ $action == delete ]]; then
    python3 "$INVENTORY_CORE" color delete "$provider" "$uuid" >/dev/null || {
      sk_lock_release 9
      sk_die "could not reset the exact session color; nothing changed"
      return 1
    }
  else
    python3 "$INVENTORY_CORE" color set "$provider" "$uuid" "$chosen" \
      >/dev/null || {
      sk_lock_release 9
      sk_die "could not set the exact session color; nothing changed"
      return 1
    }
  fi
  sk_lock_release 9
  if [[ $action == delete ]]; then
    printf 'Reset color for exact %s session %s (stable hash color applies)\n' \
      "$provider" "$id"
  else
    printf 'Colored exact %s session %s: %s\n' "$provider" "$id" "$chosen"
  fi
  printf 'Claude sessions show the color natively from their next start or resume.\n'
}

update_ai_alias() {
  local action=$1 provider=$2 uuid=$3 title=${4:-}
  case "$action" in
    set)
      python3 "$INVENTORY_CORE" alias set "$provider" "$uuid" -- "$title" >/dev/null
      ;;
    delete)
      python3 "$INVENTORY_CORE" alias delete "$provider" "$uuid" >/dev/null
      ;;
    *)
      return 2
      ;;
  esac
}

name_target() {
  local selector=$1 action=$2 title=${3:-}
  resolve_target "$selector" || return 1
  sk_require_mutation_target || return 1
  [[ $SK_CONVERSATION_PROVIDER == claude || $SK_CONVERSATION_PROVIDER == codex ]] || {
    sk_die "managed shell labels are derived and cannot be renamed"
    return 1
  }
  local id=$SK_ID started=$SK_STARTED
  local provider=$SK_CONVERSATION_PROVIDER uuid=$SK_CONVERSATION_UUID
  local live_provider=$SK_PROVIDER live_uuid=$SK_UUID
  local shell_pid=$SK_SHELL_PID shell_start=$SK_SHELL_START
  local provider_pid=$SK_PROVIDER_PID provider_start=$SK_PROVIDER_START
  local daemon_pid=$SK_DAEMON_PID daemon_start=$SK_DAEMON_START
  picker_lock || return 1
  sk_revalidate "$id" "$started" "$live_provider" "$live_uuid" \
    "$shell_pid" "$shell_start" "$provider_pid" "$provider_start" \
    "$daemon_pid" "$daemon_start" "$provider" "$uuid" || {
      sk_lock_release 9
      sk_die "target changed before name update; nothing changed"
      return 1
    }
  update_ai_alias "$action" "$provider" "$uuid" "$title" || {
    sk_lock_release 9
    sk_die "could not update the exact session name; nothing changed"
    return 1
  }
  sk_lock_release 9
  if [[ $action == delete ]]; then
    printf 'Reset local name for exact %s session %s\n' "$provider" "$id"
  else
    printf 'Named exact %s session %s\n' "$provider" "$id"
  fi
}

find_history() {
  local query=$1
  [[ -n $query ]] || return 1
  local found=0 path
  while IFS= read -r path; do
    [[ -r $path ]] || continue
    if LC_ALL=C grep -aFi -- "$query" "$path" >/dev/null 2>&1; then
      printf '\n%s\n' "$path"
      LC_ALL=C grep -aFi -- "$query" "$path" | head -8
      found=1
    fi
  done < <(
    find "$SK_JOURNAL_DIR" -type f -name '*.raw' -print 2>/dev/null
    find "$SK_RECOVERY_DIR" -type f -name '*.raw' -print 2>/dev/null
  )
  while IFS= read -r path; do
    [[ -r $path ]] || continue
    if gzip -dc "$path" 2>/dev/null | LC_ALL=C grep -aFi -- "$query" >/dev/null 2>&1; then
      printf '\n%s\n' "$path"
      gzip -dc "$path" 2>/dev/null | LC_ALL=C grep -aFi -- "$query" | head -8
      found=1
    fi
  done < <(find "$SK_ARCHIVE_DIR" -type f -name '*.gz' -print 2>/dev/null)
  (( found == 1 )) || echo "Nothing found."
}

show_health() {
  sk_require_shpool || return 1
  local daemon
  daemon=$(pgrep -f '^.*/shpool daemon$' | head -1)
  if [[ -z $daemon ]]; then
    echo "shpool daemon: unavailable"
    return 1
  fi
  ps -o pid=,rss=,nlwp=,etime=,command= -p "$daemon"
  printf 'Host memory:\n'
  free -h 2>/dev/null || true
}

show_recovery() {
  local manifest="$SK_STATE_DIR/recovery-manifest.json"
  [[ -r $manifest ]] || {
    echo "No exact recovery manifest is available."
    return 0
  }
  python3 - "$manifest" <<'PY'
import json, sys
d=json.load(open(sys.argv[1]))
rows=(d.get("sessions") or {}).values()
for i,row in enumerate(rows,1):
    print(f"{i:3}  {row.get('provider','unknown'):6}  {row.get('title','')}  {row.get('uuid','')}")
PY
  echo "Recovery launches only through the reviewed login chooser."
}

verify_start_target() {
  local selector=$1
  sk_require_integration || return 1
  resolve_target "$selector" || return 1
  sk_require_mutation_target || return 1
  local id=$SK_ID
  sk_read_start_expectation "$id" || {
    sk_die "no complete retained launch proof exists for that session"
    return 1
  }
  local expected_provider=$SK_EXPECT_PROVIDER expected_cwd=$SK_EXPECT_CWD
  local expected_uuid=$SK_EXPECT_UUID expected_started=$SK_EXPECT_STARTED
  local expected_boot_id=$SK_EXPECT_BOOT_ID
  local expected_shell_pid=$SK_EXPECT_SHELL_PID expected_shell_start=$SK_EXPECT_SHELL_START
  local expected_daemon_pid=$SK_EXPECT_DAEMON_PID expected_daemon_start=$SK_EXPECT_DAEMON_START
  local SESSION_KIT_PROVIDER_PROOF_ATTEMPTS=1
  if sk_wait_for_provider "$id" "$expected_provider" "$expected_cwd" "$expected_uuid" \
    "$expected_boot_id" "$expected_started" "$expected_shell_pid" "$expected_shell_start" \
    "$expected_daemon_pid" "$expected_daemon_start" "$SK_EXPECT_LAUNCH_MODE"; then
    printf 'Verified exact %s provider and cleared retained launch record for %s\n' "$expected_provider" "$id"
    return 0
  fi
  sk_die "exact provider is not active yet; record retained. Attach with 'sp go $id' and run 'exec bash -i' to retry"
}
