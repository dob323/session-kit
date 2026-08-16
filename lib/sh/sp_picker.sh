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
  # The same terminal binding attach_id does, for the same reason: this is the
  # door the login menu and the TUI picker attach through, and BOTH known ways
  # of handing down a non-terminal descriptor reach it -- the stale /dev/null
  # left on the picker's stderr by shpool_login_live.sh:376, and the TUI
  # launcher's stderr FIFO (bin/shpool_login_launcher:75). X19 lane finding F1
  # was this pair drifting apart -- a change to one belongs in the other.
  #
  # SCOPED here, unlike attach_id: that one execs, so its binding dies with the
  # process, while this one returns and an unscoped binding outlived the attach
  # -- everything `sp` printed afterwards bypassed the launcher's capture and
  # landed on the screen out of order. Armed here, released on every exit path
  # below and in `sp`'s traps.
  sk_handoff_guard_arm "$id"
  sk_push_session_color "${SK_PROOF_PROVIDER:-}" "${SK_PROOF_UUID:-}"
  # The picker's own open and takeover come through here rather than through
  # attach_id, so the kit's tab name is written here too -- with the same kill
  # switch and the same scrub, instead of a raw write that honoured neither.
  sk_tab_title "$(sk_human_label "${SK_TITLE:-}" "${SK_PROVIDER:-}" "${SK_NUMBER:-}")"
  # The scrollback refill must cover this door too: the login menu and the TUI
  # picker attach through here, not through attach_id (X19 lane finding F1).
  # Before the first attempt, so the 0.7s retry below never replays twice.
  sk_replay_history "$id"
  assert_input_modes
  if [[ $force == 1 ]] &&
     ! sk_shpool_holder_is "$SK_PROOF_DAEMON_PID" "$SK_PROOF_DAEMON_START"; then
    sk_handoff_guard_release 1
    sk_die "the socket author changed before takeover; nothing was moved"
    return 1
  fi
  if "$SK_SHPOOL" "${args[@]}"; then
    sk_handoff_guard_release 0
    return 0
  fi
  # One retry after a beat: takeovers and just-released sessions race the
  # daemon's teardown of the previous client, and the action log shows those
  # first attempts failing regularly.
  sleep 0.7
  assert_input_modes
  local sk_attach_rc=0
  if [[ $force == 1 ]] &&
     ! sk_shpool_holder_is "$SK_PROOF_DAEMON_PID" "$SK_PROOF_DAEMON_START"; then
    sk_handoff_guard_release 1
    sk_die "the socket author changed before takeover retry; nothing was moved"
    return 1
  fi
  "$SK_SHPOOL" "${args[@]}" || sk_attach_rc=$?
  # The client has exited and been waited on, so the terminal is ours to check.
  # A session that died badly leaves it raw -- unusable, no echo -- and this is
  # where that gets put right instead of the person discovering it.
  sk_handoff_guard_release "$sk_attach_rc"
  return $sk_attach_rc
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
    printf 'Restarted the idle Codex provider for %s; its conversation and shell were preserved.\n' \
      "$(sk_human_label "$SK_TITLE" "$SK_PROVIDER")"
    return 0
  fi
  if picker_bounce_claude "$SK_ID" explicit; then
    printf 'Restarted the idle Claude provider for %s; its conversation and shell were preserved.\n' \
      "$(sk_human_label "$SK_TITLE" "$SK_PROVIDER")"
    return 0
  fi
  picker_action_event title_refresh refused
  sk_die "the provider title is not pending or the provider is not proven idle or awaiting a reply with no subagents; nothing restarted"
  return "$PICKER_REFUSED_STATUS"
}

# The picker already proved the exact displayed row.  Reuse the public model
# verb for every safety check and for the actual restart; this adapter only
# converts the picker's private proof into that verb's exact selector.
picker_change_model() {
  local proof=$1 requested=$2
  picker_action_event model_change requested
  picker_load_fresh "$proof" || {
    picker_action_event model_change refused
    return "$PICKER_REFUSED_STATUS"
  }
  local id=$SK_ID
  local status=0
  SESSION_KIT_CONFIRM_ID=$id change_model_target "$id" "$requested" || status=$?
  if (( status == 0 )); then
    picker_action_event model_change returned
  else
    picker_action_event model_change failed
  fi
  return "$status"
}

picker_open() {
  local proof=$1
  picker_action_event picker_open requested
  picker_load_fresh "$proof" || {
    picker_action_event picker_open refused
    return "$PICKER_REFUSED_STATUS"
  }
  [[ $SK_STATUS == disconnected || $SK_STATUS == Disconnected ]] || {
    sk_die "$(sk_human_label "$SK_TITLE" "$SK_PROVIDER") is already open in another window; nothing changed"
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
  # A name first: it is the common case and it applies the colour too, since
  # the window restarts either way. If no name can be applied but a recolour
  # is still waiting -- the session was busy or attached when `sp color` asked
  # -- the colour gets its own restart, which is the one an unnamed session
  # could never get.
  if ! picker_bounce_codex "$id" automatic && ! picker_bounce_claude "$id" automatic; then
    if [[ -f $SK_STATE_DIR/provider-recolor/$id ]]; then
      picker_bounce_codex "$id" automatic color ||
        picker_bounce_claude "$id" automatic color || true
    fi
  fi
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
    sk_die "$(sk_human_label "$SK_TITLE" "$SK_PROVIDER") is available; use open instead"
    picker_action_event picker_takeover state_changed
    return "$PICKER_REFUSED_STATUS"
  }
  local id=$SK_ID title=$SK_TITLE provider=$SK_PROVIDER
  sk_confirm_exact "Move session to this window" "$id" "$title" "$provider" \
    "${SK_NUMBER:-}" || {
    echo "Nothing changed."
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
  sk_prepare_state && sk_load_picker_proof "$proof" && make_guard_snapshot &&
    { sk_picker_proof_matches "$SNAPSHOT" ||
      sk_picker_proof_matches_dead_shell "$SNAPSHOT"; } || {
    sk_die "displayed session changed; nothing changed"
    picker_action_event picker_close refused
    return 1
  }
  local id=$SK_ID title=$SK_TITLE provider=$SK_PROVIDER
  sk_confirm_exact "Close session and everything running inside it" \
    "$id" "$title" "$provider" "${SK_NUMBER:-}" || {
    echo "Nothing changed."
    picker_action_event picker_close cancelled
    return 1
  }
  picker_lock || return 1
  if ! picker_revalidate_locked &&
     ! sk_picker_proof_matches_dead_shell "$SNAPSHOT"; then
    sk_lock_release 9
    sk_die "displayed session changed while confirming; nothing closed"
    picker_action_event picker_close state_changed
    return 1
  fi
  local shell_outcome manager_cleanup=0
  if ! shell_outcome=$(sk_terminate_exact_shell \
      "$SK_PROOF_DAEMON_PID" "$SK_PROOF_DAEMON_START" \
      "$SK_PROOF_SHELL_PID" "$SK_PROOF_SHELL_START" 2>&1); then
    sk_cleanup_dead_shpool_entry "$id" \
      "$SK_PROOF_DAEMON_PID" "$SK_PROOF_DAEMON_START" \
      "$SK_PROOF_SHELL_PID" "$SK_PROOF_SHELL_START" \
      "$SK_PROOF_STARTED" || true
    sk_lock_release 9
    sk_die "the exact displayed shell was not terminated ($shell_outcome)"
    picker_action_event picker_close failed
    return 1
  fi
  sk_cleanup_dead_shpool_entry "$id" \
    "$SK_PROOF_DAEMON_PID" "$SK_PROOF_DAEMON_START" \
    "$SK_PROOF_SHELL_PID" "$SK_PROOF_SHELL_START" \
    "$SK_PROOF_STARTED" || manager_cleanup=$?
  sk_lock_release 9
  # "Everything running inside it" must be TRUE: a provider wedged on a
  # terminal write survives the shell's SIGHUP and lingers outside shpool,
  # blocking any later reopen of the same conversation. Reap it under its
  # exact pinned identity (identity-bound no-op when it exited normally).
  sk_reap_session_leftovers \
    "${SK_PROOF_PROVIDER_PID:-}" "${SK_PROOF_PROVIDER_START:-}" \
    "${SK_PROOF_SHELL_PID:-}" "${SK_PROOF_SHELL_START:-}"
  # Somebody chose this. The crash queue must never offer it back as work
  # that was lost — a tombstone says the difference out loud, and the close
  # itself is already done, so a store that cannot be written costs a
  # diagnostic, never the close.
  #
  # The diagnostic has to reach THEM, not just the action log. An event nobody
  # reads is how `k` printed a bare "Closed …" over a conversation that had
  # landed on neither surface (found in review, 2026-08-15).
  if [[ ${SK_PROOF_PROVIDER:-} == claude || ${SK_PROOF_PROVIDER:-} == codex ]] &&
     [[ -n ${SK_PROOF_UUID:-} ]]; then
    python3 "$INVENTORY_CORE" close-intent record \
      "$SK_PROOF_PROVIDER" "$SK_PROOF_UUID" >/dev/null 2>&1 || {
      picker_action_event picker_close intent_unrecorded
      # The provider transcript survives, but `sp recover` validates the same
      # broken Closed ledger and may refuse its whole list. Say that boundary
      # rather than promising an offer the command cannot print.
      sk_die "the session closed, but it could not be added to Closed sessions — the provider conversation remains on disk, but repair the Closed sessions ledger before relying on \`sp recover\`" || true
    }
  else
    # A shell close is ledgered too, so `sp recover` lists it (history only).
    python3 "$INVENTORY_CORE" closed-sessions record shell --session "$id" \
      >/dev/null 2>&1 || {
      picker_action_event picker_close intent_unrecorded
      sk_die "the session closed, but it could not be added to Closed sessions" || true
    }
  fi
  # A delegated session was given its own copy of the code; the close gives it
  # back, and says so when it keeps one because of unmerged work.
  sk_release_worktree "$id"
  if (( manager_cleanup != 0 )); then
    picker_action_event picker_close manager_entry_remains
    sk_die "the exact shell closed, but manager entry $id remains; Session Kit could not finish the close"
    return 1
  fi
  picker_action_event picker_close closed
  printf 'Closed %s\n' "$(sk_human_label "$title" "$SK_PROOF_PROVIDER")"
}

# Shared tail after an exact disconnected session has been killed while the
# create lock was held. Reap anything it left holding the old terminal, then
# relaunch the same exact conversation. `restore_exact` takes the lock itself.
# True while an exact conversation is still visible anywhere in the live
# inventory, including as a process outside shpool.
conversation_is_active() {
  local provider=$1 uuid=$2 stale_id=${3:-}
  make_guard_snapshot || return 2
  local answer
  answer=$(python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
wanted_provider=sys.argv[2]
wanted_uuid=sys.argv[3].lower()
stale_id=sys.argv[4]
rows=[
    row for row in d.get("sessions",[])
    if not stale_id or row.get("shpool_id_raw") != stale_id
]
rows+=d.get("outside_agents",[])
print("yes" if any(
    isinstance(row,dict)
    and row.get("provider")==wanted_provider
    and isinstance((row.get("identity") or {}).get("uuid"),str)
    and row["identity"]["uuid"].lower()==wanted_uuid
    for row in rows
) else "no")
' "$SNAPSHOT" "$provider" "$uuid" "$stale_id") || return 2
  [[ $answer == yes ]]
}

finish_recovery_after_kill() {
  local id=$1 provider=$2 uuid=$3 cwd=$4
  local shell_pid=$5 shell_start=$6 provider_pid=$7 provider_start=$8
  local origin SK_RESTORE_IGNORE_STALE_ID=$id
  # An unstamped session declares NOTHING rather than declaring "human": a
  # declaration outranks the closed-session record, so inventing one here
  # would overwrite what the ledger knows about the conversation being
  # reopened. Empty leaves the restore to read that record as usual.
  origin=$(sk_session_origin "$SNAPSHOT" "$id") || origin=
  sk_reap_session_leftovers \
    "$provider_pid" "$provider_start" \
    "$shell_pid" "$shell_start"
  # A provider wedged on a terminal write can outlive its shell by a few
  # seconds. Relaunching while its identity is still visible would be refused
  # as a duplicate conversation -- which would leave the session closed and
  # nothing brought back. Wait for it to disappear first.
  local attempt
  for attempt in {1..60}; do
    conversation_is_active "$provider" "$uuid" "$id" || break
    sleep 1
  done
  if conversation_is_active "$provider" "$uuid" "$id"; then
    sk_die "the session was closed, but its conversation is still held by a live process; run 'sp recover' once it exits"
    return 1
  fi
  SESSION_KIT_ORIGIN=$origin restore_exact "$provider" "$uuid" "$cwd"
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
  local shell_outcome
  if ! shell_outcome=$(sk_terminate_exact_shell \
      "$SK_PROOF_DAEMON_PID" "$SK_PROOF_DAEMON_START" \
      "$shell_pid" "$shell_start" 2>&1); then
    sk_cleanup_dead_shpool_entry "$id" \
      "$SK_PROOF_DAEMON_PID" "$SK_PROOF_DAEMON_START" \
      "$shell_pid" "$shell_start" "$SK_PROOF_STARTED" || true
    sk_lock_release 9
    sk_die "the exact disconnected shell was not terminated; nothing changed ($shell_outcome)"
    picker_action_event picker_recover failed
    return 1
  fi
  sk_cleanup_dead_shpool_entry "$id" \
    "$SK_PROOF_DAEMON_PID" "$SK_PROOF_DAEMON_START" \
    "$shell_pid" "$shell_start" "$SK_PROOF_STARTED" || {
      sk_lock_release 9
      return 1
    }
  sk_lock_release 9
  if finish_recovery_after_kill "$id" "$provider" "$uuid" "$cwd" \
    "$shell_pid" "$shell_start" "$provider_pid" "$provider_start"; then
    picker_action_event picker_recover recovered
    return 0
  fi
  picker_action_event picker_recover failed
  echo "The original session was already closed. The conversation is intact on disk." >&2
  echo "Retry with: sp restore-exact $provider $uuid $cwd" >&2
  return 1
}

picker_history() {
  local proof=$1
  picker_load_fresh "$proof" || return 1
  show_history_id "$SK_PROOF_ID"
}

account_switch_stable_snapshot() {
  local snapshot_path=$1 id=$2 uuid=$3 provider=$4 expected_alias=${5:-}
  python3 - "$snapshot_path" "$id" "$uuid" "$provider" "$expected_alias" <<'PY'
import json
import sys

data=json.load(open(sys.argv[1], encoding="utf-8"))
matches=[row for row in data.get("sessions", []) if row.get("shpool_id_raw")==sys.argv[2]]
if len(matches) != 1:
    raise SystemExit(1)
row=matches[0]
identity=row.get("identity") or {}
if (
    row.get("provider") != sys.argv[4]
    or identity.get("uuid") != sys.argv[3]
    or row.get("mutation_allowed") is not True
    or row.get("subagents")
    or int(row.get("active_subagent_count") or 0) != 0
):
    raise SystemExit(1)
if sys.argv[5] and (
    row.get("account_alias") != sys.argv[5]
    or row.get("account_binding_mismatch") is True
):
    raise SystemExit(1)
if str(row.get("agent_status") or "").casefold() not in {
    "idle", "needs your reply", "reply optional"
}:
    raise SystemExit(1)
age=row.get("recent_output_age_seconds")
if isinstance(age,int) and not isinstance(age,bool) and age < 5:
    raise SystemExit(1)
PY
}

account_switch_safe_tree() {
  local provider=$1 shell_pid=$2 shell_start=$3 provider_pid=$4 provider_start=$5
  local table
  table=$(mktemp "$SK_STATE_DIR/account-switch-processes.XXXXXX") || return 1
  if ! python3 "$INVENTORY_CORE" platform process-table >"$table" 2>/dev/null ||
     ! python3 - "$table" "$provider" "$shell_pid" "$shell_start" \
       "$provider_pid" "$provider_start" <<'PY'
import json
import os
import sys

path,provider,shell_pid,shell_start,provider_pid,provider_start=sys.argv[1:]
shell_pid, shell_start, provider_pid, provider_start=map(
    int, (shell_pid,shell_start,provider_pid,provider_start)
)
rows=json.load(open(path, encoding="utf-8")).get("processes",[])
processes={row.get("pid"):row for row in rows if isinstance(row,dict)}
if processes.get(shell_pid,{}).get("start_ticks") != shell_start:
    raise SystemExit(1)
if processes.get(provider_pid,{}).get("start_ticks") != provider_start:
    raise SystemExit(1)
children={}
for pid,row in processes.items():
    children.setdefault(row.get("ppid"),[]).append(pid)
queue=list(children.get(shell_pid,[]))
seen=set()
while queue:
    pid=queue.pop()
    if pid in seen:
        continue
    seen.add(pid)
    queue.extend(children.get(pid,[]))
for pid in seen:
    row=processes.get(pid,{})
    argv=[str(value) for value in (row.get("cmdline") or [])]
    executable=os.path.basename(argv[0]) if argv else str(row.get("comm") or "")
    args=" ".join(argv[1:])
    if pid == provider_pid:
        continue
    allowed=(
        executable in {
            "script",
            "mcp@latest",
            "npm exec @playwright/mcp@latest",
            "codex-code-mode-host",
        }
        or (executable in {"bash", "-bash"} and not any(value in {"-c", "-lc"} for value in argv[1:]))
        or (executable == "shpool" and "attach" in argv and "--cmd" in argv)
        or (executable == "node" and ("/codex" in args or "playwright-mcp" in args))
        or (provider == "codex" and executable == "codex")
        or (executable in {"python", "python3"} and "provider_broker.py" in args)
        # A provider's own hook children: Claude Code spawns a project's
        # .claude/hooks/ scripts itself, so one of them running is the
        # provider working, not an unrecognized background job. Refusing on
        # them made a switch impossible in any project with a resident hook.
        or (executable in {"python", "python3"} and "/.claude/hooks/" in args)
    )
    if not allowed:
        raise SystemExit(1)
PY
  then
    command rm -f -- "$table"
    return 1
  fi
  command rm -f -- "$table"
}

account_switch_request() {
  local id=$1 action=$2 txid=$3 alias=$4 fallback=$5 uuid=$6
  local started=$7 shell_pid=$8 shell_start=$9
  local model=${10:-} provider=${11:-} validated=
  local directory="$SK_STATE_DIR/account-switch-requests"
  [[ $id =~ ^(main([1-9][0-9]*)?|s[0-9]{8}-[0-9]{6}-[1-9][0-9]*(-[1-9][0-9]*)?)$ &&
     $action =~ ^(apply|rollback)$ && $txid =~ ^[0-9a-f]{32}$ &&
     $alias =~ ^[a-z][a-z0-9_-]{0,11}$ &&
     $fallback =~ ^[a-z][a-z0-9_-]{0,11}$ &&
     $uuid =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ &&
     $started =~ ^[1-9][0-9]*$ && $shell_pid =~ ^[1-9][0-9]*$ &&
     $shell_start =~ ^[1-9][0-9]*$ && $provider =~ ^(claude|codex)$ &&
     $model != *[$'\t\r\n\034']* ]] || return 1
  if [[ -n $model ]]; then
    validated=$(python3 "$INVENTORY_CORE" validate-worker-model \
      "$provider" "$model" 2>/dev/null) || return 1
    [[ $validated == "$model" ]] || return 1
  fi
  umask 077
  mkdir -p -- "$directory" || return 1
  chmod 700 -- "$directory" || return 1
  local temporary="$directory/.${id}.$$"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$action" "$txid" "$alias" "$fallback" "$uuid" "$started" \
    "$shell_pid" "$shell_start" "$model" > "$temporary" || return 1
  chmod 600 -- "$temporary" || return 1
  mv -f -- "$temporary" "$directory/$id"
}

account_switch_signal() {
  local provider=$1 provider_pid=$2 provider_start=$3 shell_pid=$4 shell_start=$5
  local signal_pid=$provider_pid signal_start=$provider_start target
  if [[ $provider == codex ]]; then
    target=$(python3 "$INVENTORY_CORE" platform codex-refresh-target \
      "$shell_pid" "$shell_start" "$provider_pid" "$provider_start" 2>/dev/null) || return 1
    IFS=$'\t' read -r signal_pid signal_start <<<"$target"
  fi
  python3 "$INVENTORY_CORE" platform process-is \
    "$signal_pid" "$signal_start" "$provider" >/dev/null 2>&1 || return 1
  kill -TERM "$signal_pid"
}

account_switch_wait_alias() {
  local id=$1 provider=$2 uuid=$3 alias=$4 attempt fresh
  for attempt in {1..150}; do
    fresh=$(mktemp "$SK_STATE_DIR/account-switch-wait.XXXXXX") || return 1
    if sk_guard_snapshot_file "$fresh" 2>/dev/null &&
       python3 - "$fresh" "$id" "$provider" "$uuid" "$alias" <<'PY'
import json,sys
data=json.load(open(sys.argv[1], encoding="utf-8"))
rows=[row for row in data.get("sessions",[]) if row.get("shpool_id_raw")==sys.argv[2]]
if len(rows)!=1:
    raise SystemExit(1)
row=rows[0]
identity=row.get("identity") or {}
raise SystemExit(0 if (
    row.get("provider")==sys.argv[3]
    and identity.get("uuid")==sys.argv[4]
    and row.get("account_alias")==sys.argv[5]
    and row.get("account_switch_capable") is True
) else 1)
PY
    then
      command rm -f -- "$fresh"
      return 0
    fi
    command rm -f -- "$fresh"
    sleep 0.2
  done
  return 1
}

account_switch_restore_source() {
  local id=$1 provider=$2 uuid=$3 source_alias=$4 target_alias=$5 txid=$6 cwd=$7
  # `--no-recreate` stops before the destructive last resort below. A person
  # driving the picker is present and wants the conversation back whatever it
  # costs, so the default is unchanged; an unattended caller must not close a
  # live session and hope the recreate lands, because the promise it is held
  # to is that a failed handoff leaves the operator still working.
  # The opt-out is read from anywhere in the argument list, never from one
  # fixed position. Carrying the model added an argument in front of it, and a
  # positional read would have turned that refactor into a silent re-arming of
  # the kill below -- with every source-text test still green, because the flag
  # is still written at the call site. Position is not what makes it safe.
  local model=${8:-}
  local SK_RESTORE_IGNORE_STALE_ID=
  local allow_recreate=1 argument
  for argument in "$@"; do
    [[ $argument != --no-recreate ]] || allow_recreate=0
  done
  # An older caller that puts the flag where the model now goes must not have
  # it forwarded onward as a model identifier.
  [[ $model != --no-recreate ]] || model=
  local fresh current_alias current_pid current_start current_shell_pid current_shell_start
  local current_started
  local current_daemon_pid current_daemon_start
  fresh=$(mktemp "$SK_STATE_DIR/account-switch-rollback.XXXXXX") || return 1
  if sk_guard_snapshot_file "$fresh" 2>/dev/null && sk_resolve "$fresh" "$id" &&
     [[ $SK_PROVIDER == "$provider" && $SK_UUID == "$uuid" ]]; then
    current_alias=$SK_ACCOUNT_ALIAS
    current_started=$SK_STARTED
    current_pid=$SK_PROVIDER_PID
    current_start=$SK_PROVIDER_START
    current_shell_pid=$SK_SHELL_PID
    current_shell_start=$SK_SHELL_START
    current_daemon_pid=$SK_DAEMON_PID
    current_daemon_start=$SK_DAEMON_START
    command rm -f -- "$fresh"
    if [[ $current_alias == "$source_alias" ]]; then
      return 0
    fi
    if [[ $current_alias == "$target_alias" ]] &&
       account_switch_request "$id" rollback "$txid" "$source_alias" \
         "$target_alias" "$uuid" "$SK_STARTED" "$current_shell_pid" \
         "$current_shell_start" "$model" "$provider" &&
       account_switch_signal "$provider" "$current_pid" "$current_start" \
         "$current_shell_pid" "$current_shell_start" &&
       account_switch_wait_alias "$id" "$provider" "$uuid" "$source_alias"; then
      return 0
    fi
  else
    command rm -f -- "$fresh"
  fi
  # The managed provider did not survive long enough to consume a rollback
  # request. Recreate only this exact shell and conversation on the source.
  (( allow_recreate )) || return 1
  [[ $current_shell_pid =~ ^[0-9]+$ && $current_shell_start =~ ^[0-9]+$ &&
     $current_daemon_pid =~ ^[0-9]+$ && $current_daemon_start =~ ^[0-9]+$ ]] ||
    return 1
  sk_terminate_exact_shell "$current_daemon_pid" "$current_daemon_start" \
    "$current_shell_pid" "$current_shell_start" >/dev/null 2>&1 || return 1
  sk_cleanup_dead_shpool_entry "$id" "$current_daemon_pid" "$current_daemon_start" \
    "$current_shell_pid" "$current_shell_start" "$current_started" || return 1
  SK_RESTORE_IGNORE_STALE_ID=$id
  sk_reap_session_leftovers "${current_pid:-}" "${current_start:-}" \
    "${current_shell_pid:-}" "${current_shell_start:-}"
  python3 "$INVENTORY_CORE" account switch-rollback "$txid" >/dev/null 2>&1 || true
  restore_exact "$provider" "$uuid" "$cwd" "$source_alias" "$model" >/dev/null 2>&1
}

picker_account_switch() {
  local proof=$1 target_alias=$2
  picker_action_event account_switch requested
  [[ ! -e $SK_STATE_DIR/account-switching-off ]] || {
    sk_die "account switching is disabled by its kill switch; nothing changed"
    return "$PICKER_REFUSED_STATUS"
  }
  [[ $target_alias =~ ^[a-z][a-z0-9_-]{0,11}$ ]] || {
    sk_die "account alias is invalid; nothing changed"
    return "$PICKER_REFUSED_STATUS"
  }
  picker_load_fresh "$proof" || return "$PICKER_REFUSED_STATUS"
  [[ $SK_PROVIDER == claude || $SK_PROVIDER == codex ]] || {
    sk_die "only an exact Claude or Codex conversation can change account"
    return "$PICKER_REFUSED_STATUS"
  }
  local provider=$SK_PROVIDER uuid=$SK_UUID id=$SK_ID cwd=$SK_CWD model=$SK_MODEL
  local SK_RESTORE_IGNORE_STALE_ID=
  [[ $uuid =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || return "$PICKER_REFUSED_STATUS"
  account_switch_stable_snapshot "$SNAPSHOT" "$id" "$uuid" "$provider" || {
    sk_die "the conversation is working or its state is unproven; nothing changed"
    return "$PICKER_REFUSED_STATUS"
  }
  account_switch_safe_tree "$provider" "$SK_PROOF_SHELL_PID" "$SK_PROOF_SHELL_START" \
    "$SK_PROOF_PROVIDER_PID" "$SK_PROOF_PROVIDER_START" || {
    sk_die "the managed shell has an active or unrecognized background child; nothing changed"
    return "$PICKER_REFUSED_STATUS"
  }
  local source_json source_alias
  source_json=$(python3 "$INVENTORY_CORE" account source "$provider" "$uuid" 2>/dev/null) || {
    sk_die "the conversation's current account could not be uniquely proven; nothing changed"
    return "$PICKER_REFUSED_STATUS"
  }
  source_alias=$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("alias", ""))' <<<"$source_json" 2>/dev/null) || return "$PICKER_REFUSED_STATUS"
  [[ $source_alias =~ ^[a-z][a-z0-9_-]{0,11}$ && $source_alias != "$target_alias" ]] || {
    sk_die "the selected account is already active or the source changed; nothing changed"
    return "$PICKER_REFUSED_STATUS"
  }
  local switch_error
  switch_error=$(mktemp) || return "$PICKER_REFUSED_STATUS"
  python3 "$INVENTORY_CORE" account launch-profile "$provider" "$target_alias" \
    >/dev/null 2>"$switch_error" || {
    sk_die "$(grep -m1 . "$switch_error" 2>/dev/null ||
      echo "the target account could not be prepared; nothing changed")"
    command rm -f -- "$switch_error"
    return "$PICKER_REFUSED_STATUS"
  }
  command rm -f -- "$switch_error"
  sk_confirm_exact "Change account from $source_alias to $target_alias" \
    "$id" "$SK_TITLE" "$provider" "${SK_NUMBER:-}" || {
    echo "Nothing changed."
    return "$PICKER_REFUSED_STATUS"
  }
  picker_lock || return "$PICKER_REFUSED_STATUS"
  if ! picker_revalidate_locked ||
     ! account_switch_stable_snapshot "$SNAPSHOT" "$id" "$uuid" "$provider" ||
     ! account_switch_safe_tree "$provider" "$SK_PROOF_SHELL_PID" "$SK_PROOF_SHELL_START" \
       "$SK_PROOF_PROVIDER_PID" "$SK_PROOF_PROVIDER_START"; then
    sk_lock_release 9
    sk_die "the displayed conversation changed; nothing changed"
    return "$PICKER_REFUSED_STATUS"
  fi
  local prepared txid
  prepared=$(python3 "$INVENTORY_CORE" account switch-prepare \
    "$provider" "$uuid" "$source_alias" "$target_alias" "$cwd" "$id" 2>/dev/null) || {
    sk_lock_release 9
    sk_die "the account checkpoint could not be prepared; nothing changed"
    return "$PICKER_REFUSED_STATUS"
  }
  txid=$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("txid", ""))' <<<"$prepared" 2>/dev/null) || txid=
  [[ $txid =~ ^[0-9a-f]{32}$ ]] || {
    sk_lock_release 9
    sk_die "the account checkpoint identity was invalid; nothing changed"
    return "$PICKER_REFUSED_STATUS"
  }
  local capable=$SK_ACCOUNT_CAPABLE
  local shell_pid=$SK_PROOF_SHELL_PID shell_start=$SK_PROOF_SHELL_START
  local provider_pid=$SK_PROOF_PROVIDER_PID provider_start=$SK_PROOF_PROVIDER_START
  if [[ $capable == true ]]; then
    account_switch_request "$id" apply "$txid" "$target_alias" "$source_alias" \
      "$uuid" "$SK_PROOF_STARTED" "$shell_pid" "$shell_start" "$model" \
      "$provider" || {
      sk_lock_release 9
      python3 "$INVENTORY_CORE" account switch-rollback "$txid" >/dev/null 2>&1 || true
      sk_die "the account handoff request could not be written; nothing changed"
      return "$PICKER_REFUSED_STATUS"
    }
    if ! account_switch_signal "$provider" "$provider_pid" "$provider_start" \
         "$shell_pid" "$shell_start"; then
      command rm -f -- "$SK_STATE_DIR/account-switch-requests/$id"
      sk_lock_release 9
      python3 "$INVENTORY_CORE" account switch-rollback "$txid" >/dev/null 2>&1 || true
      sk_die "the exact idle provider changed before handoff; nothing changed"
      return "$PICKER_REFUSED_STATUS"
    fi
    sk_lock_release 9
    if account_switch_wait_alias "$id" "$provider" "$uuid" "$target_alias" &&
       python3 "$INVENTORY_CORE" account sync-ui "$provider" "$uuid" \
         "$target_alias" >/dev/null &&
       python3 "$INVENTORY_CORE" account switch-commit "$txid" >/dev/null; then
      picker_action_event account_switch committed
      printf 'Changed session %s from %s to %s. The conversation and shell were kept.\n' \
        "${SK_NUMBER:-$id}" "$source_alias" "$target_alias"
      return 0
    fi
    if account_switch_restore_source "$id" "$provider" "$uuid" "$source_alias" \
         "$target_alias" "$txid" "$cwd" "$model"; then
      sk_die "the target account did not prove itself; the original account was restored"
      picker_action_event account_switch rolled_back
    else
      sk_die "the target account did not prove itself and the original account could not be verified; use sp recover before opening it"
      picker_action_event account_switch rollback_failed
    fi
    return 1
  fi

  # Legacy shells cannot reload a new profile in place. Their first switch is
  # the approved one-time recreation; the exact provider UUID keeps the same
  # terminal number, while the old window returns to Session Kit.
  #
  # Read the model BEFORE the kill: it is on the live provider's command line,
  # so it stops existing the moment the process does. A recreation that does
  # not carry it hands the conversation back on the provider's own default,
  # which is the kit changing a model by itself with nothing on screen.
  local carried_model=
  carried_model=$(sk_session_model "$SNAPSHOT" "$id") || carried_model=
  # Two branches carried the model across this handoff and neither is
  # redundant: `carried_model` is read from the live provider's own command
  # line just above, which is the truthful answer, while `$model` is what the
  # picker's row said on entry. Prefer the live reading and fall back to the
  # row, so a failed read degrades to a worse answer rather than to none --
  # silently starting a conversation on a different model is the trap this
  # whole gate exists to close.
  [[ -n $carried_model ]] || carried_model=$model
  local shell_outcome
  if ! shell_outcome=$(sk_terminate_exact_shell \
      "$SK_PROOF_DAEMON_PID" "$SK_PROOF_DAEMON_START" \
      "$shell_pid" "$shell_start" 2>&1); then
    sk_cleanup_dead_shpool_entry "$id" \
      "$SK_PROOF_DAEMON_PID" "$SK_PROOF_DAEMON_START" \
      "$shell_pid" "$shell_start" "$SK_PROOF_STARTED" || true
    sk_lock_release 9
    python3 "$INVENTORY_CORE" account switch-rollback "$txid" >/dev/null 2>&1 || true
    sk_die "the legacy shell could not be closed safely; nothing changed ($shell_outcome)"
    return "$PICKER_REFUSED_STATUS"
  fi
  sk_cleanup_dead_shpool_entry "$id" \
    "$SK_PROOF_DAEMON_PID" "$SK_PROOF_DAEMON_START" \
    "$shell_pid" "$shell_start" "$SK_PROOF_STARTED" || {
      sk_lock_release 9
      return "$PICKER_REFUSED_STATUS"
    }
  SK_RESTORE_IGNORE_STALE_ID=$id
  sk_lock_release 9
  sk_reap_session_leftovers "$provider_pid" "$provider_start" "$shell_pid" "$shell_start"
  local attempt
  for attempt in {1..60}; do
    conversation_is_active "$provider" "$uuid" "$id" || break
    sleep 0.2
  done
  if conversation_is_active "$provider" "$uuid" "$id" ||
     ! python3 "$INVENTORY_CORE" account switch-apply "$txid" >/dev/null 2>&1; then
    python3 "$INVENTORY_CORE" account switch-rollback "$txid" >/dev/null 2>&1 || true
    restore_exact "$provider" "$uuid" "$cwd" "$source_alias" "$carried_model" \
      >/dev/null 2>&1 || true
    sk_die "the legacy account handoff failed; Session Kit attempted to restore the original account"
    return 1
  fi
  local restored_id
  restored_id=$(restore_exact "$provider" "$uuid" "$cwd" "$target_alias" \
    "$carried_model") || {
    python3 "$INVENTORY_CORE" account switch-rollback "$txid" >/dev/null 2>&1 || true
    restore_exact "$provider" "$uuid" "$cwd" "$source_alias" "$carried_model" \
      >/dev/null 2>&1 || true
    sk_die "the target account did not start; Session Kit attempted to restore the original account"
    return 1
  }
  if ! python3 "$INVENTORY_CORE" account sync-ui "$provider" "$uuid" \
       "$target_alias" >/dev/null ||
     ! python3 "$INVENTORY_CORE" account switch-commit "$txid" >/dev/null; then
    sk_die "the target account started but final verification failed; it and the pending checkpoint were left in place because no exact process proof was available for a safe rollback close"
    return 1
  fi
  picker_action_event account_switch committed_legacy
  printf 'Changed session %s from %s to %s. Reopen the same number once in the original window.\n' \
    "${SK_NUMBER:-$restored_id}" "$source_alias" "$target_alias"
  # The model is part of what the operator is looking at. Say which one came
  # back, and say it plainly when none could be carried, rather than leaving
  # them to notice later that the session is answering differently.
  if [[ -n $carried_model ]]; then
    printf 'It comes back on %s, the model it was on.\n' "$carried_model"
  else
    printf 'It was not on a named model, so it comes back on %s'"'"'s own default.\n' \
      "$provider"
  fi
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
    printf 'Reset the local name for the exact %s session\n' "$SK_PROOF_PROVIDER"
  else
    printf 'Named the exact %s session %s\n' "$SK_PROOF_PROVIDER" \
      "$(sk_human_label "$title" "$SK_PROOF_PROVIDER")"
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
  if ! sk_attach_new_unique "$id" "$cwd"; then
    id=$SK_ATTACHED_ID
    local quarantine
    quarantine=$(sk_quarantine_start_record "$id" attach-failed 2>/dev/null || true)
    sk_lock_release 9
    sk_die "shpool did not confirm fork shell creation; launch record quarantined at ${quarantine:-$SK_START_DIR/failed}"
    return 1
  fi
  id=$SK_ATTACHED_ID
  if ! sk_capture_session_generation "$id" "$creation_floor_ms"; then
    local quarantine
    quarantine=$(sk_quarantine_start_record "$id" generation-unproven 2>/dev/null || true)
    sk_lock_release 9
    sk_die "the fork shell may be open, but its exact generation was not proven; unarmed record quarantined at ${quarantine:-$SK_START_DIR/failed}"
    return 1
  fi
  local created_started=$SK_CREATED_STARTED
  local created_boot_id=$SK_CREATED_BOOT_ID
  local created_shell_pid=$SK_CREATED_SHELL_PID created_shell_start=$SK_CREATED_SHELL_START
  local created_daemon_pid=$SK_CREATED_DAEMON_PID created_daemon_start=$SK_CREATED_DAEMON_START
  # A fork is stamped only after its exact shell generation is captured. A
  # momentarily unstamped row stays visible; a name-only stamp could hide a
  # different session that later reuses this manager name.
  sk_record_origin "$id" "$(sk_environment_origin)" "$created_started" \
    "$created_shell_pid" "$created_shell_start"
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
    sk_die "the fork session remains open, but a distinct exact $provider conversation was not proven; fork launch record retained"
    return 1
  fi
  printf 'Forked %s into a separate %s session\n' \
    "$(sk_human_label "$SK_TITLE" "$provider")" "$provider"
  # No identifier here or anywhere else a person reads: the fork's exact
  # proof lives in its 0600 launch record and the JSON views.
}
