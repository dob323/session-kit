#!/usr/bin/env bash
# sp actions the login picker calls with a proof: attach, open, take over,
# close, recover, history, alias, and fork. Every one of these revalidates the
# picker proof under the create lock before it changes anything. Source this
# file; do not execute it.
#
# Source order: bin/sp sources this module ahead of its first trap, after
# SCRIPT_DIR, session_kit_common, INVENTORY_CORE, PICKER_REFUSED_STATUS, and
# PICKER_ATTACH_FAILED_STATUS are in place. These functions call the snapshot
# and attach helpers in lib/sh/sp_core.sh, the provider bounce in
# lib/sh/sp_provider_bounce.sh, and the session builders in
# lib/sh/sp_sessions.sh.
#
# Globals the entry script owns are assigned there, not here.
# shellcheck disable=SC2154

picker_attach_id() {
  local id=$1 cwd=${2:-} force=${3:-0}
  local -a args=(attach)
  [[ $force == 1 ]] && args+=(--force)
  args+=(--cmd /bin/false)
  [[ -z $cwd ]] || args+=(--dir "$cwd")
  args+=("$id")
  sk_push_session_color "${SK_PROOF_PROVIDER:-}" "${SK_PROOF_UUID:-}"
  assert_input_modes
  if "$SK_SHPOOL" "${args[@]}"; then
    return 0
  fi
  # One retry after a beat: takeovers and just-released sessions race the
  # daemon's teardown of the previous client, and the action log shows those
  # first attempts failing regularly.
  sleep 0.7
  assert_input_modes
  "$SK_SHPOOL" "${args[@]}"
}

picker_action_event() {
  sk_log_action "$1" "$2" || true
}

picker_load_fresh() {
  local proof=$1
  sk_prepare_state || return 1
  sk_load_picker_proof "$proof" || return 1
  make_guard_snapshot || {
    sk_die "guard live inventory unavailable; nothing changed"
    return 1
  }
  sk_picker_proof_matches "$SNAPSHOT" || {
    sk_die "displayed session changed; nothing changed"
    return 1
  }
}

picker_lock() {
  exec 9>"$SK_STATE_DIR/create.lock" || return 1
  sk_lock_acquire 9 "$SK_STATE_DIR/create.lock" || {
    sk_lock_release 9
    return 1
  }
}

picker_revalidate_locked() {
  make_guard_snapshot || return 1
  sk_picker_proof_matches "$SNAPSHOT"
}

picker_refresh_title() {
  local proof=$1
  picker_action_event title_refresh requested
  picker_load_fresh "$proof" || {
    picker_action_event title_refresh refused
    return "$PICKER_REFUSED_STATUS"
  }
  if picker_bounce_codex "$SK_ID" explicit; then
    printf 'Restarted the idle Codex provider for exact session %s; its conversation and shell were preserved.\n' "$SK_ID"
    return 0
  fi
  if picker_bounce_claude "$SK_ID" explicit; then
    printf 'Restarted the idle Claude provider for exact session %s; its conversation and shell were preserved.\n' "$SK_ID"
    return 0
  fi
  picker_action_event title_refresh refused
  sk_die "the provider title is not pending or the provider is not proven idle or awaiting a reply with no subagents; nothing restarted"
  return "$PICKER_REFUSED_STATUS"
}

picker_open() {
  local proof=$1
  picker_action_event picker_open requested
  picker_load_fresh "$proof" || {
    picker_action_event picker_open refused
    return "$PICKER_REFUSED_STATUS"
  }
  [[ $SK_STATUS == disconnected || $SK_STATUS == Disconnected ]] || {
    sk_die "$SK_PROOF_ID is already open in another window; nothing changed"
    picker_action_event picker_open state_changed
    return "$PICKER_REFUSED_STATUS"
  }
  picker_lock || {
    picker_action_event picker_open refused
    return "$PICKER_REFUSED_STATUS"
  }
  if ! picker_revalidate_locked ||
     [[ $SK_STATUS != disconnected && $SK_STATUS != Disconnected ]]; then
    sk_lock_release 9
    sk_die "displayed session changed before open; nothing changed"
    picker_action_event picker_open state_changed
    return "$PICKER_REFUSED_STATUS"
  fi
  local id=$SK_ID cwd=$SK_CWD
  # Keeping this descriptor across exec would block all managed creation for
  # the lifetime of the attachment. Release it immediately before the guarded
  # name-only request; --cmd /bin/false prevents a persistent raced replacement.
  sk_lock_release 9
  picker_bounce_codex "$id" automatic || picker_bounce_claude "$id" automatic || true
  if picker_attach_id "$id" "$cwd"; then
    picker_action_event picker_open returned
    return 0
  fi
  picker_action_event picker_open attach_failed
  sk_die "the session manager could not connect to the selected session; its cause was not identified and nothing was called dead"
  return "$PICKER_ATTACH_FAILED_STATUS"
}

picker_takeover() {
  local proof=$1
  picker_action_event picker_takeover requested
  picker_load_fresh "$proof" || {
    picker_action_event picker_takeover refused
    return "$PICKER_REFUSED_STATUS"
  }
  [[ $SK_STATUS != disconnected && $SK_STATUS != Disconnected ]] || {
    sk_die "$SK_PROOF_ID is available; use open instead"
    picker_action_event picker_takeover state_changed
    return "$PICKER_REFUSED_STATUS"
  }
  local id=$SK_ID title=$SK_TITLE provider=$SK_PROVIDER
  sk_confirm_exact "Move session to this window" "$id" "$title" "$provider" || {
    echo "Move cancelled."
    picker_action_event picker_takeover cancelled
    return "$PICKER_REFUSED_STATUS"
  }
  picker_lock || {
    picker_action_event picker_takeover refused
    return "$PICKER_REFUSED_STATUS"
  }
  if ! picker_revalidate_locked ||
     [[ $SK_STATUS == disconnected || $SK_STATUS == Disconnected ]]; then
    sk_lock_release 9
    sk_die "displayed session changed while confirming; nothing changed"
    picker_action_event picker_takeover state_changed
    return "$PICKER_REFUSED_STATUS"
  fi
  local cwd=$SK_CWD
  sk_lock_release 9
  if picker_attach_id "$id" "$cwd" 1; then
    picker_action_event picker_takeover returned
    return 0
  fi
  picker_action_event picker_takeover attach_failed
  sk_die "the session manager could not move the selected session; the other window was not described as dead"
  return "$PICKER_ATTACH_FAILED_STATUS"
}

picker_close() {
  local proof=$1
  picker_action_event picker_close requested
  picker_load_fresh "$proof" || {
    picker_action_event picker_close refused
    return 1
  }
  local id=$SK_ID title=$SK_TITLE provider=$SK_PROVIDER
  sk_confirm_exact "Close session and everything running inside it" \
    "$id" "$title" "$provider" || {
    echo "Close cancelled."
    picker_action_event picker_close cancelled
    return 1
  }
  picker_lock || return 1
  if ! picker_revalidate_locked; then
    sk_lock_release 9
    sk_die "displayed session changed while confirming; nothing closed"
    picker_action_event picker_close state_changed
    return 1
  fi
  sk_kill_status=0
  sk_timeout 20 "$SK_SHPOOL" kill "$id" || sk_kill_status=$?
  if (( sk_kill_status == 124 || sk_kill_status == 137 )); then
    sk_die "the close did not confirm within 20s — the session may or may not have ended; refresh the list before acting again"
    sk_lock_release 9 2>/dev/null
    return 1
  fi
  if (( sk_kill_status != 0 )); then
    sk_lock_release 9
    sk_die "shpool refused to close the exact displayed session"
    picker_action_event picker_close failed
    return 1
  fi
  sk_lock_release 9
  # "Everything running inside it" must be TRUE: a provider wedged on a
  # terminal write survives the shell's SIGHUP and lingers outside shpool,
  # blocking any later reopen of the same conversation. Reap it under its
  # exact pinned identity (identity-bound no-op when it exited normally).
  sk_reap_session_leftovers \
    "${SK_PROOF_PROVIDER_PID:-}" "${SK_PROOF_PROVIDER_START:-}" \
    "${SK_PROOF_SHELL_PID:-}" "${SK_PROOF_SHELL_START:-}"
  picker_action_event picker_close closed
  printf 'Closed exact session %s\n' "$id"
}

# Shared tail after an exact disconnected session has been killed while the
# create lock was held. Reap anything it left holding the old terminal, then
# relaunch the same exact conversation. `restore_exact` takes the lock itself.
# True while an exact conversation is still visible anywhere in the live
# inventory, including as a process outside shpool.
conversation_is_active() {
  local provider=$1 uuid=$2
  make_guard_snapshot || return 2
  local answer
  answer=$(python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
wanted_provider=sys.argv[2]
wanted_uuid=sys.argv[3].lower()
rows=d.get("sessions",[])+d.get("outside_agents",[])
print("yes" if any(
    isinstance(row,dict)
    and row.get("provider")==wanted_provider
    and isinstance((row.get("identity") or {}).get("uuid"),str)
    and row["identity"]["uuid"].lower()==wanted_uuid
    for row in rows
) else "no")
' "$SNAPSHOT" "$provider" "$uuid") || return 2
  [[ $answer == yes ]]
}

finish_recovery_after_kill() {
  local id=$1 provider=$2 uuid=$3 cwd=$4
  local shell_pid=$5 shell_start=$6 provider_pid=$7 provider_start=$8
  sk_reap_session_leftovers \
    "$provider_pid" "$provider_start" \
    "$shell_pid" "$shell_start"
  # A provider wedged on a terminal write can outlive `shpool kill` by a few
  # seconds. Relaunching while its identity is still visible would be refused
  # as a duplicate conversation -- which would leave the session closed and
  # nothing brought back. Wait for it to disappear first.
  local attempt
  for attempt in {1..60}; do
    conversation_is_active "$provider" "$uuid" || break
    sleep 1
  done
  if conversation_is_active "$provider" "$uuid"; then
    sk_die "session $id was closed, but conversation $uuid is still held by a live process; run 'sp recover' once it exits"
    return 1
  fi
  restore_exact "$provider" "$uuid" "$cwd"
}

# Picker path: the displayed session could not be opened. Bound to the private
# proof, so it can only ever act on the exact row the user selected.
picker_recover() {
  local proof=$1
  picker_action_event picker_recover requested
  sk_require_integration || {
    picker_action_event picker_recover refused
    return 1
  }
  picker_load_fresh "$proof" || {
    picker_action_event picker_recover refused
    return 1
  }
  [[ $SK_PROOF_PROVIDER == claude || $SK_PROOF_PROVIDER == codex ]] || {
    sk_die "only an exact Claude or Codex conversation can be moved to a new session"
    return 1
  }
  local provider=$SK_PROOF_PROVIDER uuid=$SK_PROOF_UUID
  [[ $uuid =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || {
    sk_die "recovery requires an exact canonical conversation UUID"
    return 1
  }
  picker_lock || return 1
  if ! picker_revalidate_locked; then
    sk_lock_release 9
    sk_die "displayed session changed before recovery; nothing changed"
    picker_action_event picker_recover state_changed
    return 1
  fi
  if [[ $SK_STATUS != disconnected && $SK_STATUS != Disconnected ]]; then
    sk_lock_release 9
    sk_die "the selected session is open in another window; recovery refused and nothing changed"
    picker_action_event picker_recover attached
    return 1
  fi
  local id=$SK_ID cwd=$SK_CWD
  local shell_pid=$SK_PROOF_SHELL_PID shell_start=$SK_PROOF_SHELL_START
  local provider_pid=$SK_PROOF_PROVIDER_PID provider_start=$SK_PROOF_PROVIDER_START
  [[ $cwd == /* && -d $cwd ]] || {
    sk_lock_release 9
    sk_die "session directory is not an existing absolute directory"
    return 1
  }
  cwd=$(cd -- "$cwd" && pwd -P) || {
    sk_lock_release 9
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
    sk_die "shpool refused to close the exact disconnected session; nothing changed"
    picker_action_event picker_recover failed
    return 1
  fi
  sk_lock_release 9
  if finish_recovery_after_kill "$id" "$provider" "$uuid" "$cwd" \
    "$shell_pid" "$shell_start" "$provider_pid" "$provider_start"; then
    picker_action_event picker_recover recovered
    return 0
  fi
  picker_action_event picker_recover failed
  return 1
}

picker_history() {
  local proof=$1
  picker_load_fresh "$proof" || return 1
  show_history_id "$SK_PROOF_ID"
}

picker_alias() {
  local proof=$1 action=$2 title=${3:-}
  picker_load_fresh "$proof" || return 1
  [[ $SK_PROOF_PROVIDER == claude || $SK_PROOF_PROVIDER == codex ]] || {
    sk_die "managed shell labels are derived and cannot be renamed"
    return 1
  }
  picker_lock || return 1
  if ! picker_revalidate_locked; then
    sk_lock_release 9
    sk_die "displayed session changed before name update; nothing changed"
    return 1
  fi
  update_ai_alias "$action" "$SK_PROOF_PROVIDER" "$SK_PROOF_UUID" "$title" || {
    sk_lock_release 9
    sk_die "could not update the exact session name; nothing changed"
    return 1
  }
  sk_lock_release 9
  if [[ $action == delete ]]; then
    printf 'Reset local name for exact %s session %s\n' "$SK_PROOF_PROVIDER" "$SK_PROOF_ID"
  else
    printf 'Named exact %s session %s\n' "$SK_PROOF_PROVIDER" "$SK_PROOF_ID"
  fi
}

picker_fork() {
  local proof=$1
  sk_require_integration || return 1
  picker_load_fresh "$proof" || return 1
  [[ $SK_PROOF_PROVIDER == claude || $SK_PROOF_PROVIDER == codex ]] || {
    sk_die "only exact Claude or Codex sessions can be forked"
    return 1
  }
  local provider=$SK_PROOF_PROVIDER source_uuid=$SK_PROOF_UUID
  [[ $source_uuid =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || {
    sk_die "fork requires an exact canonical source UUID"
    return 1
  }
  picker_lock || return 1
  if ! sk_require_integration; then
    sk_lock_release 9
    return 1
  fi
  if ! picker_revalidate_locked; then
    sk_lock_release 9
    sk_die "displayed session changed before fork; nothing launched"
    return 1
  fi
  local cwd=$SK_CWD
  [[ $cwd == /* && -d $cwd ]] || {
    sk_lock_release 9
    sk_die "fork source directory is not an existing absolute directory"
    return 1
  }
  cwd=$(cd -- "$cwd" && pwd -P) || {
    sk_lock_release 9
    return 1
  }

  local id
  id=$(sk_allocate_id) || {
    sk_lock_release 9
    sk_die "could not allocate a unique fork session ID"
    return 1
  }
  sk_write_start_record "$id" "$provider" "$cwd" "$source_uuid" fork || {
    sk_lock_release 9
    sk_die "could not write the exact fork launch record"
    return 1
  }
  local creation_floor_ms
  creation_floor_ms=$(sk_now_unix_ms) || {
    sk_lock_release 9
    return 1
  }
  if ! sk_timeout 30 "$SK_SHPOOL" attach --background --dir "$cwd" "$id"; then
    local quarantine
    quarantine=$(sk_quarantine_start_record "$id" attach-failed 2>/dev/null || true)
    sk_lock_release 9
    sk_die "shpool did not confirm fork shell creation; launch record quarantined at ${quarantine:-$SK_START_DIR/failed}"
    return 1
  fi
  if ! sk_capture_session_generation "$id" "$creation_floor_ms"; then
    local quarantine
    quarantine=$(sk_quarantine_start_record "$id" generation-unproven 2>/dev/null || true)
    sk_lock_release 9
    sk_die "fork shell $id may be open, but its exact generation was not proven; unarmed record quarantined at ${quarantine:-$SK_START_DIR/failed}"
    return 1
  fi
  local created_started=$SK_CREATED_STARTED
  local created_boot_id=$SK_CREATED_BOOT_ID
  local created_shell_pid=$SK_CREATED_SHELL_PID created_shell_start=$SK_CREATED_SHELL_START
  local created_daemon_pid=$SK_CREATED_DAEMON_PID created_daemon_start=$SK_CREATED_DAEMON_START
  sk_write_generation_record "$id" "$provider" "$cwd" "$source_uuid" \
    "$created_boot_id" "$created_started" "$created_shell_pid" "$created_shell_start" \
    "$created_daemon_pid" "$created_daemon_start" fork || {
      local quarantine
      quarantine=$(sk_quarantine_start_record "$id" arming-failed 2>/dev/null || true)
      sk_lock_release 9
      sk_die "fork generation could not be armed; record quarantined at ${quarantine:-$SK_START_DIR/failed}"
      return 1
    }
  sk_lock_release 9
  if ! sk_wait_for_provider "$id" "$provider" "$cwd" "$source_uuid" \
    "$created_boot_id" "$created_started" "$created_shell_pid" "$created_shell_start" \
    "$created_daemon_pid" "$created_daemon_start" fork; then
    sk_die "session $id remains open, but a distinct exact $provider fork was not proven; fork launch record retained"
    return 1
  fi
  printf 'Forked exact %s conversation %s into session %s as %s\n' \
    "$provider" "$source_uuid" "$id" "$SK_PROVEN_UUID"
}
