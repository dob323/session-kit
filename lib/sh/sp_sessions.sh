#!/usr/bin/env bash
# sp session builders: repair a broken session, pre-bake a Claude conversation,
# create a new session, and restore an exact recorded one. Source this file; do
# not execute it.
#
# Source order: bin/sp sources this module ahead of its first trap, after
# SCRIPT_DIR, session_kit_common, and INVENTORY_CORE are in place. These
# functions call the snapshot and attach helpers in lib/sh/sp_core.sh and read
# SNAPSHOT, which those helpers assign.
#
# Globals the entry script owns are assigned there, not here.
# shellcheck disable=SC2154

# Watchdog path: same repair, selected by session ID and never interactive.
repair_target() {
  local selector=$1
  picker_action_event repair requested
  sk_require_integration || {
    picker_action_event repair refused
    return 1
  }
  make_guard_snapshot || {
    picker_action_event repair refused
    return 1
  }
  sk_resolve "$SNAPSHOT" "$selector" || {
    sk_die "no such session: $selector"
    return 1
  }
  sk_require_mutation_target || return 1
  [[ $SK_PROVIDER == claude || $SK_PROVIDER == codex ]] || {
    sk_die "only a Claude or Codex session can be repaired"
    return 1
  }
  local id=$SK_ID provider=$SK_PROVIDER uuid=$SK_UUID cwd=$SK_CWD started=$SK_STARTED
  local shell_pid=$SK_SHELL_PID shell_start=$SK_SHELL_START
  local provider_pid=$SK_PROVIDER_PID provider_start=$SK_PROVIDER_START
  local daemon_pid=$SK_DAEMON_PID daemon_start=$SK_DAEMON_START
  [[ $uuid =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || {
    sk_die "repair requires an exact canonical conversation UUID"
    return 1
  }
  [[ $cwd == /* && -d $cwd ]] || {
    sk_die "session directory is not an existing absolute directory"
    return 1
  }
  cwd=$(cd -- "$cwd" && pwd -P) || return 1
  picker_lock || {
    picker_action_event repair refused
    return 1
  }
  sk_revalidate "$id" "$started" "$provider" "$uuid" \
    "$shell_pid" "$shell_start" "$provider_pid" "$provider_start" \
    "$daemon_pid" "$daemon_start" || {
    sk_lock_release 9
    sk_die "session changed while preparing the repair; nothing changed"
    picker_action_event repair state_changed
    return 1
  }
  if [[ $SK_STATUS != disconnected && $SK_STATUS != Disconnected ]]; then
    sk_lock_release 9
    sk_die "session is open in another window; repair refused and nothing changed"
    picker_action_event repair attached
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
    sk_die "shpool refused to close the exact disconnected session; nothing changed"
    picker_action_event repair failed
    return 1
  fi
  sk_lock_release 9
  if finish_recovery_after_kill "$id" "$provider" "$uuid" "$cwd" \
    "$shell_pid" "$shell_start" "$provider_pid" "$provider_start"; then
    picker_action_event repair recovered
    return 0
  fi
  picker_action_event repair failed
  return 1
}

# First-window color: boot a hidden throwaway TUI on a minted conversation ID,
# let it write its own native color record by typing /color into ITSELF (no
# human terminal is ever touched), then launch the real session as a resume of
# that conversation so the color is native from the first frame. Fail-open:
# any problem falls back to the plain new-session flow. Kill switches:
# SESSION_KIT_NO_PREBAKE=1 or $SK_STATE_DIR/prebake-off.
sk_prebake_claude() {
  SK_PREBAKE_UUID=
  [[ ${SESSION_KIT_NO_PREBAKE:-0} != 1 && ! -e $SK_STATE_DIR/prebake-off ]] ||
    return 1
  command -v script >/dev/null 2>&1 || return 1
  command -v claude >/dev/null 2>&1 || return 1
  # This runs BEFORE any other creation output and takes ~10s of deliberate
  # silence otherwise — long enough that humans assume a hang and start
  # pressing keys that queue into the next prompt. TTY-gated: background
  # callers keep clean output.
  [[ -t 1 ]] && printf 'Preparing a colored Claude session (~10s)...\n'
  local minted color transcript
  minted=$(python3 -c 'import uuid; print(uuid.uuid4())' 2>/dev/null) || return 1
  color=$(python3 "$INVENTORY_CORE" color conversation-pick claude "$minted" \
    2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["color"])') || return 1
  ( cd "$cwd" 2>/dev/null &&
    { sleep 4; printf '/color %s\r' "$color"; sleep 3; } |
      if [[ $(sk_platform) == Darwin ]]; then
        sk_timeout 14 env -u CLAUDE_CODE_CHILD_SESSION -u CLAUDE_CODE_SESSION_ID \
          script -q /dev/null claude --session-id "$minted"
      else
        sk_timeout 14 env -u CLAUDE_CODE_CHILD_SESSION -u CLAUDE_CODE_SESSION_ID \
          script -qec "claude --session-id $minted" /dev/null
      fi
  ) >/dev/null 2>&1 || true
  if ! transcript=$(compgen -G "$HOME/.claude/projects/*/$minted.jsonl" | head -1) ||
     [[ -z $transcript || ! -s $transcript || -L $transcript ]]; then
    python3 "$INVENTORY_CORE" color conversation-release claude "$minted" "$color" \
      >/dev/null 2>&1 || true
    return 1
  fi
  if ! python3 - "$transcript" "$color" <<'PY' >/dev/null 2>&1
import json, sys
path, expected = sys.argv[1:]
matched = False
with open(path, encoding="utf-8") as stream:
    for line in stream:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if item.get("type") == "agent-color" and item.get("agentColor") == expected:
            matched = True
raise SystemExit(0 if matched else 1)
PY
  then
    python3 "$INVENTORY_CORE" color conversation-release claude "$minted" "$color" \
      >/dev/null 2>&1 || true
    return 1
  fi
  SK_PREBAKE_UUID=$minted
}

start_new() {
  sk_require_integration || return 1
  local requested_provider=${1:-} requested_project=${2:-}
  local provider=shell cwd=$PWD project_row=""

  if [[ $requested_provider =~ ^(claude|codex|shell)$ ]]; then
    provider=$requested_provider
  elif [[ -n $requested_provider ]]; then
    requested_project=$requested_provider
    requested_provider=""
  fi

  if [[ -n $requested_project ]]; then
    project_row=$(sk_project_lookup "$requested_project") || {
      sk_die "unknown or invalid project alias: $requested_project"
      return 1
    }
    local alias configured_provider configured_cwd
    IFS=$'\t' read -r alias configured_provider configured_cwd <<<"$project_row"
    cwd=$configured_cwd
    if [[ -z $requested_provider ]]; then provider=$configured_provider; fi
  fi

  [[ -d $cwd ]] || {
    sk_die "project directory does not exist: $cwd"
    return 1
  }
  cwd=$(cd -- "$cwd" && pwd -P) || return 1

  local launch_uuid= launch_mode=new
  if [[ $provider == claude ]] && sk_prebake_claude; then
    # The window opens as a resume of the pre-baked conversation, so the
    # session color is already native in its very first frame.
    launch_uuid=$SK_PREBAKE_UUID
    launch_mode=resume
  fi
  sk_prepare_state || return 1
  exec 9>"$SK_STATE_DIR/create.lock" || return 1
  sk_lock_acquire 9 "$SK_STATE_DIR/create.lock" || { exec 9>&-; return 1; }
  if ! sk_require_integration; then
    sk_lock_release 9
    return 1
  fi
  local id
  id=$(sk_allocate_id) || {
    sk_lock_release 9
    sk_die "could not allocate a unique session ID"
    return 1
  }
  sk_write_start_record "$id" "$provider" "$cwd" "$launch_uuid" "$launch_mode" || {
    sk_lock_release 9
    return 1
  }
  printf 'Starting %s session %s in %s\n' "$provider" "$id" "$cwd"
  local creation_floor_ms
  creation_floor_ms=$(sk_now_unix_ms) || {
    sk_lock_release 9
    return 1
  }
  sk_attach_status=0
  sk_timeout 30 "$SK_SHPOOL" attach --background --dir "$cwd" "$id" || sk_attach_status=$?
  if (( sk_attach_status != 0 && sk_attach_status != 124 && sk_attach_status != 137 )); then
    local quarantine
    quarantine=$(sk_quarantine_start_record "$id" attach-failed 2>/dev/null || true)
    sk_lock_release 9
    sk_die "shpool did not confirm session creation; launch record quarantined at ${quarantine:-$SK_START_DIR/failed}"
    return 1
  fi
  # A timed-out reply (124/137) does not prove the session was NOT created —
  # generation capture below settles it either way.
  if ! sk_capture_session_generation "$id" "$creation_floor_ms"; then
    local quarantine
    quarantine=$(sk_quarantine_start_record "$id" generation-unproven 2>/dev/null || true)
    sk_lock_release 9
    sk_die "session $id may be open, but its exact generation was not proven; unarmed launch record quarantined at ${quarantine:-$SK_START_DIR/failed}"
    return 1
  fi
  local created_started=$SK_CREATED_STARTED
  local created_boot_id=$SK_CREATED_BOOT_ID
  local created_shell_pid=$SK_CREATED_SHELL_PID created_shell_start=$SK_CREATED_SHELL_START
  local created_daemon_pid=$SK_CREATED_DAEMON_PID created_daemon_start=$SK_CREATED_DAEMON_START
  sk_write_generation_record "$id" "$provider" "$cwd" "$launch_uuid" \
    "$created_boot_id" "$created_started" "$created_shell_pid" "$created_shell_start" \
    "$created_daemon_pid" "$created_daemon_start" "$launch_mode" || {
      local quarantine
      quarantine=$(sk_quarantine_start_record "$id" arming-failed 2>/dev/null || true)
      sk_lock_release 9
      sk_die "launch generation could not be armed; record quarantined at ${quarantine:-$SK_START_DIR/failed}"
      return 1
  }
  sk_lock_release 9
  if [[ $provider == shell ]] &&
     ! sk_clear_start_record "$id" "$provider" "$cwd" "$launch_uuid" \
       "$created_boot_id" "$created_started" "$created_shell_pid" \
       "$created_shell_start" "$created_daemon_pid" "$created_daemon_start" \
       "$launch_mode"; then
    sk_die "session $id is open, but its completed shell launch record could not be cleared"
    return 1
  fi
  if [[ $provider != shell ]] && ! sk_wait_for_provider "$id" "$provider" "$cwd" \
    "$launch_uuid" \
    "$created_boot_id" "$created_started" "$created_shell_pid" "$created_shell_start" \
    "$created_daemon_pid" "$created_daemon_start" "$launch_mode"; then
    sk_die "session $id remains open, but exact $provider startup was not proven; launch record retained. Attach with 'sp go $id' and run 'exec bash -i' to retry"
    return 1
  fi
  if [[ $provider != shell && -n ${SK_PROVEN_UUID:-} ]]; then
    # If Claude's first-window pre-bake could not complete, reserve a color
    # against the exact live palette now that its conversation ID is known.
    # The session is still detached here, so the native propagation lands
    # before the caller can attach. Future resumes keep the stored override.
    if [[ $provider == claude && $launch_mode == new ]]; then
      python3 "$INVENTORY_CORE" color conversation-pick claude "$SK_PROVEN_UUID" \
        >/dev/null 2>&1 || true
    fi
    # Every proven provider session gets its stable color pushed once at
    # creation, so Claude shows it natively from the first resume onward.
    python3 "$INVENTORY_CORE" color propagate "$provider" "$SK_PROVEN_UUID" \
      >/dev/null 2>&1 || true
  fi
  if [[ ${SESSION_KIT_BACKGROUND:-0} == 1 ]]; then
    printf '%s\n' "$id"
    return
  fi
  SK_PROVIDER=$provider
  SK_UUID=${SK_PROVEN_UUID:-}
  attach_id "$id"
}

restore_exact() {
  local provider=$1 uuid=$2 cwd=$3
  sk_require_integration || return 1
  [[ $provider =~ ^(claude|codex)$ ]] || {
    sk_die "restore provider must be claude or codex"
    return 1
  }
  [[ $uuid =~ ^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$ ]] || {
    sk_die "restore requires an exact conversation UUID"
    return 1
  }
  uuid=${uuid,,}
  [[ $cwd == /* && -d $cwd ]] || {
    sk_die "restore cwd is not an existing absolute directory"
    return 1
  }
  cwd=$(cd -- "$cwd" && pwd -P) || return 1

  sk_prepare_state || return 1
  exec 9>"$SK_STATE_DIR/create.lock" || return 1
  sk_lock_acquire 9 "$SK_STATE_DIR/create.lock" || { exec 9>&-; return 1; }
  if ! sk_require_integration; then
    sk_lock_release 9
    return 1
  fi
  # Recovery uses the same guard inventory as every other mutating command.
  # A half-started session that has not yet published a provider identity is
  # unrelated to this restore and must not be able to block it; the exact
  # guarantees that matter here are the duplicate-conversation check below and
  # the exact generation proof captured after creation.
  make_guard_snapshot || {
    sk_lock_release 9
    return 1
  }
  local duplicate_check
  duplicate_check=$(python3 -c '
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
  ' "$SNAPSHOT" "$provider" "$uuid") || {
    sk_lock_release 9
    sk_die "could not verify active conversation UUIDs; no recovery launched"
    return 1
  }
  if [[ $duplicate_check == yes ]]; then
    sk_lock_release 9
    sk_die "exact conversation $uuid is already active"
    return 1
  elif [[ $duplicate_check != no ]]; then
    sk_lock_release 9
    sk_die "active conversation UUID check returned an invalid result"
    return 1
  fi

  local id
  id=$(sk_allocate_id) || {
    sk_lock_release 9
    return 1
  }
  sk_write_start_record "$id" "$provider" "$cwd" "$uuid" resume || {
    sk_lock_release 9
    return 1
  }
  local creation_floor_ms
  creation_floor_ms=$(sk_now_unix_ms) || {
    sk_lock_release 9
    return 1
  }
  # Push the stable color before the provider starts so this very resume
  # already reads it from the transcript.
  python3 "$INVENTORY_CORE" color propagate "$provider" "$uuid" \
    >/dev/null 2>&1 || true
  sk_attach_status=0
  sk_timeout 30 "$SK_SHPOOL" attach --background --dir "$cwd" "$id" || sk_attach_status=$?
  if (( sk_attach_status != 0 && sk_attach_status != 124 && sk_attach_status != 137 )); then
    local quarantine
    quarantine=$(sk_quarantine_start_record "$id" attach-failed 2>/dev/null || true)
    sk_lock_release 9
    sk_die "shpool did not confirm recovery shell creation; launch record quarantined at ${quarantine:-$SK_START_DIR/failed}"
    return 1
  fi
  if ! sk_capture_session_generation "$id" "$creation_floor_ms"; then
    local quarantine
    quarantine=$(sk_quarantine_start_record "$id" generation-unproven 2>/dev/null || true)
    sk_lock_release 9
    sk_die "recovery shell $id may be open, but its exact generation was not proven; unarmed record quarantined at ${quarantine:-$SK_START_DIR/failed}"
    return 1
  fi
  sk_write_generation_record "$id" "$provider" "$cwd" "$uuid" \
    "$SK_CREATED_BOOT_ID" "$SK_CREATED_STARTED" "$SK_CREATED_SHELL_PID" "$SK_CREATED_SHELL_START" \
    "$SK_CREATED_DAEMON_PID" "$SK_CREATED_DAEMON_START" resume || {
      local quarantine
      quarantine=$(sk_quarantine_start_record "$id" arming-failed 2>/dev/null || true)
      sk_lock_release 9
      sk_die "recovery generation could not be armed; record quarantined at ${quarantine:-$SK_START_DIR/failed}"
      return 1
    }
  local created_started=$SK_CREATED_STARTED
  local created_boot_id=$SK_CREATED_BOOT_ID
  local created_shell_pid=$SK_CREATED_SHELL_PID created_shell_start=$SK_CREATED_SHELL_START
  local created_daemon_pid=$SK_CREATED_DAEMON_PID created_daemon_start=$SK_CREATED_DAEMON_START
  sk_lock_release 9
  if ! sk_wait_for_provider "$id" "$provider" "$cwd" "$uuid" \
    "$created_boot_id" "$created_started" "$created_shell_pid" "$created_shell_start" \
    "$created_daemon_pid" "$created_daemon_start" resume; then
    sk_die "session $id remains open, but exact $provider recovery was not proven; launch record retained. Attach with 'sp go $id' and run 'exec bash -i' to retry"
    return 1
  fi
  printf '%s\n' "$id"
}
