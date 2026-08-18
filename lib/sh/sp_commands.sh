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
  resolve_target "$selector" || return $?
  sk_require_mutation_target || return 1
  local id=$SK_ID started=$SK_STARTED provider=$SK_PROVIDER uuid=$SK_UUID cwd=$SK_CWD
  local shell_pid=$SK_SHELL_PID shell_start=$SK_SHELL_START
  local provider_pid=$SK_PROVIDER_PID provider_start=$SK_PROVIDER_START
  local daemon_pid=$SK_DAEMON_PID daemon_start=$SK_DAEMON_START
  if [[ $SK_STATUS != disconnected && $SK_STATUS != Disconnected ]]; then
    sk_die "$(sk_human_label "$SK_TITLE" "$SK_PROVIDER" "${SK_NUMBER:-}") is open elsewhere; 'sp takeover ${SK_NUMBER:-<session>}' moves it here."
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
  resolve_target "$selector" || return $?
  sk_require_mutation_target || return 1
  local id=$SK_ID started=$SK_STARTED provider=$SK_PROVIDER uuid=$SK_UUID title=$SK_TITLE cwd=$SK_CWD
  local shell_pid=$SK_SHELL_PID shell_start=$SK_SHELL_START
  local provider_pid=$SK_PROVIDER_PID provider_start=$SK_PROVIDER_START
  local daemon_pid=$SK_DAEMON_PID daemon_start=$SK_DAEMON_START
  if [[ $SK_STATUS == disconnected || $SK_STATUS == Disconnected ]]; then
    sk_die "$(sk_human_label "$title" "$provider" "${SK_NUMBER:-}") is ready, not open elsewhere; 'sp go ${SK_NUMBER:-<session>}' opens it here."
    return 1
  fi
  sk_confirm_exact "Taking over" "$id" "$title" "$provider" \
    "${SK_NUMBER:-}" || {
    echo "Nothing changed."
    return 1
  }
  sk_revalidate "$id" "$started" "$provider" "$uuid" "$shell_pid" "$shell_start" "$provider_pid" "$provider_start" "$daemon_pid" "$daemon_start" || {
    sk_die "target changed while confirming; no action taken"
    return 1
  }
  attach_id "$id" "$cwd" 1
}

# What model a session's provider is on, read from the row the list already
# shows. Empty when the session runs on the provider's own default, which is
# not something any surface here may invent.
sk_session_model() {
  local snapshot_path=$1 id=$2
  python3 - "$snapshot_path" "$id" <<'MODEL_PY' 2>/dev/null
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(1)
for row in data.get("sessions", []):
    if isinstance(row, dict) and row.get("shpool_id_raw") == sys.argv[2]:
        print(row.get("model") or "")
        raise SystemExit(0)
raise SystemExit(1)
MODEL_PY
}

# The STAMP on a session, for the paths that close it and open it again. It
# reads `origin_recorded`, never `origin`: `origin` is what the row IS, and
# for an unstamped session that is whatever collection inferred about who held
# a socket at that instant. Feeding an inference back through
# SESSION_KIT_ORIGIN turns it into a permanent stamp that no later refresh can
# take back -- and a repair runs precisely when a window could not be opened,
# the state most likely to read "no window". So only a real stamp is carried;
# an unstamped session prints nothing and its caller declares nothing, which
# leaves the restore to read the ledger as it would for any other restore.
sk_session_origin() {
  local snapshot_path=$1 id=$2
  python3 - "$snapshot_path" "$id" <<'ORIGIN_PY' 2>/dev/null
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(1)
for row in data.get("sessions", []):
    if isinstance(row, dict) and row.get("shpool_id_raw") == sys.argv[2]:
        value = row.get("origin_recorded")
        if value in {"human", "machine"}:
            print(value)
            raise SystemExit(0)
        raise SystemExit(1)
raise SystemExit(1)
ORIGIN_PY
}

# One deliberate close, recorded once. A conversation gets its closed-list
# entry (so a person can bring it back) and its tombstone (so the crash queue
# never offers it back as lost work) from the same call; a plain shell has no
# conversation and gets the closed-list entry alone. The close already
# happened, so a record that cannot be written costs a diagnostic, never the
# close -- but the diagnostic has to say where the conversation actually IS,
# because that is the whole reason the person is reading it.
sk_record_close() {
  local id=$1 provider=$2 uuid=$3
  if [[ ($provider == claude || $provider == codex) && -n $uuid ]]; then
    python3 "$INVENTORY_CORE" close-intent record "$provider" "$uuid" \
      >/dev/null 2>&1 && return 0
    # The verb writes the findable row first and refuses the tombstone if it
    # did not land. The provider transcript survives, but the fused recovery
    # command also validates the broken Closed ledger and may refuse its whole
    # list. Name that boundary instead of promising an offer it cannot print.
    sk_die "the session closed, but it could not be added to Closed sessions — the provider conversation remains on disk, but repair the Closed sessions ledger before relying on \`sp recover\`" || true
    return 0
  fi
  python3 "$INVENTORY_CORE" closed-sessions record shell --session "$id" \
    >/dev/null 2>&1 && return 0
  # A plain shell has no conversation to lose, only its history line. Still
  # said out loud: swallowing it made `sp recover` quietly incomplete.
  sk_die "the session closed, but it could not be added to Closed sessions" || true
  return 0
}

close_target() {
  local selector=$1
  resolve_target "$selector" || return $?
  sk_require_mutation_target || return 1
  local id=$SK_ID started=$SK_STARTED provider=$SK_PROVIDER uuid=$SK_UUID title=$SK_TITLE
  local number=${SK_NUMBER:-}
  local shell_pid=$SK_SHELL_PID shell_start=$SK_SHELL_START
  local provider_pid=$SK_PROVIDER_PID provider_start=$SK_PROVIDER_START
  local daemon_pid=$SK_DAEMON_PID daemon_start=$SK_DAEMON_START
  sk_confirm_exact "Closing" "$id" "$title" "$provider" "$number" || {
    echo "Nothing changed."
    return 1
  }
  exec 9>"$SK_STATE_DIR/create.lock" || return 1
  sk_lock_acquire 9 "$SK_STATE_DIR/create.lock" || {
    sk_lock_release 9
    return 1
  }
  sk_revalidate "$id" "$started" "$provider" "$uuid" "$shell_pid" "$shell_start" "$provider_pid" "$provider_start" "$daemon_pid" "$daemon_start" || {
    sk_lock_release 9
    sk_die "target changed while confirming; no action taken"
    return 1
  }
  local shell_outcome manager_cleanup=0
  if ! shell_outcome=$(sk_terminate_exact_shell \
      "$daemon_pid" "$daemon_start" "$shell_pid" "$shell_start" 2>&1); then
    sk_cleanup_dead_shpool_entry "$id" "$daemon_pid" "$daemon_start" \
      "$shell_pid" "$shell_start" "$started" || true
    sk_lock_release 9
    sk_die "the exact session shell was not terminated ($shell_outcome)"
    return 1
  fi
  sk_cleanup_dead_shpool_entry "$id" "$daemon_pid" "$daemon_start" \
    "$shell_pid" "$shell_start" "$started" || manager_cleanup=$?
  sk_lock_release 9
  # Same guarantee as the picker path: nothing pinned to this session's
  # exact identity may outlive the close.
  sk_reap_session_leftovers \
    "${provider_pid:-}" "${provider_start:-}" \
    "${shell_pid:-}" "${shell_start:-}"
  # And the same record of intent. `sp close` is a person's explicit verb
  # exactly as `k` is, so the conversation it ends must not come back as an
  # unclaimed crash. The close already happened: a store that cannot be
  # written costs a warning, never the close.
  sk_record_close "$id" "$provider" "${uuid:-}"
  # The copy of the code a delegated session was given goes back on the close
  # itself, rather than waiting for somebody to remember `sp teardown`.
  sk_release_worktree "$id"
  if (( manager_cleanup != 0 )); then
    sk_die "the exact shell closed, but manager entry $id remains; Session Kit could not finish the close"
    return 1
  fi
  if [[ -e $SK_START_DIR/$id || -L $SK_START_DIR/$id ||
        -e $SK_START_DIR/$id.expected || -L $SK_START_DIR/$id.expected ]]; then
    if ! sk_quarantine_start_record "$id" closed >/dev/null; then
      sk_die "the session was closed, but its retained launch record could not be archived"
      return 1
    fi
  fi
  printf 'Closed %s.\n' "$(sk_human_label "${title:-}" "${provider:-}" "${number:-}")"
}

# Move one conversation to another model. The provider is restarted on the
# model asked for and resumes the exact same conversation, so nothing about
# the work is lost -- but a restart in the middle of a turn would be, which is
# why this refuses on anything but a proven idle session. The proof is the one
# the account switch already uses: a single stable row, a conversation that is
# not mid-computation, no subagents, and a process tree with nothing
# unrecognized in it. Nothing is ever asked (D7); automation names the exact
# session, exactly as it does to close one.
change_model_target() {
  local selector=$1 requested=$2 anyway=${3:-}
  [[ -n $requested ]] || {
    sk_die "change-model needs a session and a model"
    return 2
  }
  local model_anyway=0
  [[ $anyway != --model-anyway ]] || model_anyway=1
  resolve_target "$selector" || return $?
  sk_require_mutation_target || return 1
  [[ $SK_PROVIDER == claude || $SK_PROVIDER == codex ]] || {
    sk_die "only a Claude or Codex session runs on a model"
    return 1
  }
  local id=$SK_ID started=$SK_STARTED provider=$SK_PROVIDER uuid=$SK_UUID
  local cwd=$SK_CWD title=$SK_TITLE number=${SK_NUMBER:-}
  local account_alias=${SK_ACCOUNT_ALIAS:-}
  local shell_pid=$SK_SHELL_PID shell_start=$SK_SHELL_START
  local provider_pid=$SK_PROVIDER_PID provider_start=$SK_PROVIDER_START
  local daemon_pid=$SK_DAEMON_PID daemon_start=$SK_DAEMON_START
  [[ -n $uuid ]] || {
    sk_die "that session has no conversation to carry to another model"
    return 1
  }
  local model
  model=$(python3 "$INVENTORY_CORE" validate-worker-model \
    "$provider" "$requested" 2>/dev/null) || {
      sk_die "unsupported or unsafe $provider model identifier"
      return 2
    }
  [[ $model == "$requested" ]] || {
    sk_die "model validation did not preserve the identifier"
    return 2
  }
  # The same question `sp new` asks: does this machine actually answer that
  # request with that model? A move onto a model that is really served by
  # something else is a downgrade nobody sees, so it is refused by name and
  # the conversation stays exactly where it is.
  if (( model_anyway == 0 )); then
    local availability_message= availability_status=0
    availability_message=$(python3 "$INVENTORY_CORE" model-availability \
      "$provider" "$model" --flag=--model-anyway 2>&1 >/dev/null) ||
      availability_status=$?
    if (( availability_status == 3 )); then
      [[ -z $availability_message ]] || printf '%s\n' "$availability_message" >&2
      sk_die "nothing changed"
      return 2
    fi
    if (( availability_status != 0 )); then
      # Say what the check itself said before replacing it with a summary: a
      # broken gate must not look like a missing one.
      [[ -z $availability_message ]] || printf '%s\n' "$availability_message" >&2
      printf 'session-kit: could not check whether this machine serves %s; moving to it as asked.\n' \
        "$model" >&2
    elif [[ -n $availability_message && -t 2 ]]; then
      # "Nothing here can confirm that model" is not a refusal and never stops
      # the move. A person is told at the moment they asked; automation asks
      # `session-kit model-availability` when it wants the verdict.
      printf '%s\n' "$availability_message" >&2
    fi
  fi
  local current
  current=$(sk_session_model "$SNAPSHOT" "$id") || current=""
  local origin
  # Same rule as the recovery path: only a real stamp is carried forward. An
  # unstamped session declares nothing, and the restore reads the ledger.
  origin=$(sk_session_origin "$SNAPSHOT" "$id") || origin=
  # ``current`` is only the model recorded on the conversation's last reply.
  # It cannot prove what the live process is running after an in-session
  # model command. An explicit correction therefore follows the normal proof
  # and restart path even when the last reply happens to name the requested
  # model; silently accepting stale evidence would decline the person's act.
  account_switch_stable_snapshot "$SNAPSHOT" "$id" "$uuid" "$provider" || {
    sk_die "that session is working, so its model was not changed; try again when it is idle"
    return 1
  }
  account_switch_safe_tree "$provider" "$shell_pid" "$shell_start" \
    "$provider_pid" "$provider_start" || {
    sk_die "something the kit does not recognize is running in that session, so its model was not changed"
    return 1
  }
  sk_confirm_exact "Moving" "$id" "$title" "$provider" "$number" || {
    echo "Nothing changed."
    return 1
  }
  # Time passes between the first safety proof and this destructive step; a
  # new turn can begin in that interval. Recollect once and prove exact
  # identity, idle state, and the recognized process tree again from that same
  # fresh snapshot.
  exec 9>"$SK_STATE_DIR/create.lock" || return 1
  sk_lock_acquire 9 "$SK_STATE_DIR/create.lock" || {
    sk_lock_release 9
    return 1
  }
  make_guard_snapshot || {
    sk_lock_release 9
    sk_die "guard live inventory unavailable; nothing changed"
    return 1
  }
  sk_snapshot_matches_exact "$SNAPSHOT" \
    "$id" "$started" "$provider" "$uuid" "$shell_pid" "$shell_start" \
    "$provider_pid" "$provider_start" "$daemon_pid" "$daemon_start" || {
    sk_lock_release 9
    sk_die "session changed while preparing the model change; nothing changed"
    return 1
  }
  account_switch_stable_snapshot "$SNAPSHOT" "$id" "$uuid" "$provider" || {
    sk_lock_release 9
    sk_die "that session started working while its model was being chosen, so nothing changed"
    return 1
  }
  account_switch_safe_tree "$provider" "$SK_SHELL_PID" "$SK_SHELL_START" \
    "$SK_PROVIDER_PID" "$SK_PROVIDER_START" || {
    sk_lock_release 9
    sk_die "something the kit does not recognize started in that session, so its model was not changed"
    return 1
  }
  local shell_outcome
  if ! shell_outcome=$(sk_terminate_exact_shell \
      "$daemon_pid" "$daemon_start" "$shell_pid" "$shell_start" 2>&1); then
    sk_cleanup_dead_shpool_entry "$id" "$daemon_pid" "$daemon_start" \
      "$shell_pid" "$shell_start" "$started" || true
    sk_lock_release 9
    sk_die "the exact session shell could not be stopped; nothing changed ($shell_outcome)"
    return 1
  fi
  sk_cleanup_dead_shpool_entry "$id" "$daemon_pid" "$daemon_start" \
    "$shell_pid" "$shell_start" "$started" || {
      sk_lock_release 9
      return 1
    }
  sk_lock_release 9
  sk_reap_session_leftovers \
    "${provider_pid:-}" "${provider_start:-}" \
    "${shell_pid:-}" "${shell_start:-}"
  # The old session ended on purpose, so the crash queue must never offer its
  # conversation back as lost work. The restore below takes the same
  # conversation out of the closed list again.
  sk_record_close "$id" "$provider" "$uuid"
  if ! SESSION_KIT_ORIGIN=$origin \
    restore_exact "$provider" "$uuid" "$cwd" "$account_alias" "$model" >/dev/null; then
    sk_die "the session closed, but it could not be reopened on $model. Its conversation is intact; restore it from the picker"
    return 1
  fi
  printf 'Restored %s on %s.\n' \
    "$(sk_human_label "${title:-}" "$provider")" "$model"
}

# One top-level string from a JSON object, stripped of anything that is not
# printable. The verdict's sentence is printed to a terminal and handed to the
# operator's notifier, so a feed that ever carried an escape sequence must not
# be able to paint either one.
sk_json_field() {
  local payload=$1 key=$2
  printf '%s' "$payload" | python3 -c '
import json
import sys

try:
    value = json.load(sys.stdin)
except ValueError:
    raise SystemExit(1)
if not isinstance(value, dict):
    raise SystemExit(1)
text = value.get(sys.argv[1])
if text is None:
    raise SystemExit(1)
text = "".join(c if c.isprintable() else " " for c in str(text))
print(" ".join(text.split())[:240])
' "$key"
}

# Carry one conversation off an account that has run dry, without being asked.
#
# The decision is not made here. `account auto-plan` reads the usage feed, the
# reserve, the one-move limit and the kill switch and answers switch or hold;
# this function performs a "switch" answer and nothing else. Keeping the two
# apart is what makes the rule testable without ever moving a real account.
#
# Every safety proof below is the manual switch's own, called by name:
# account_switch_stable_snapshot, account_switch_safe_tree,
# account_switch_request, account_switch_signal, account_switch_wait_alias and
# account_switch_restore_source. The automatic path can therefore never be
# laxer than the path a human drives -- it can only be refused earlier.
#
# Only a session whose provider can reload a profile in place is ever moved.
# The legacy path recreates the shell and asks the operator to reopen the same
# number in the old window; performing that while nobody is watching would
# leave a moved conversation behind an instruction no one read.
account_auto_switch_target() {
  # Judging is the default; acting needs `--apply`. This verb moves a live
  # conversation between paid subscriptions, and it is reachable from any
  # shell in this checkout, so the form that costs money must be the one
  # somebody typed on purpose rather than the one they get by accident.
  local selector=$1 dry_run=1
  [[ ${2:-} != --apply ]] || dry_run=
  resolve_target "$selector" || return $?
  sk_require_mutation_target || return 1
  [[ $SK_PROVIDER == claude || $SK_PROVIDER == codex ]] || {
    sk_die "only a Claude or Codex conversation runs on an account"
    return 1
  }
  local id=$SK_ID started=$SK_STARTED provider=$SK_PROVIDER uuid=$SK_UUID
  local cwd=$SK_CWD title=$SK_TITLE number=${SK_NUMBER:-}
  local capable=${SK_ACCOUNT_CAPABLE:-}
  local model=${SK_MODEL:-}
  local shell_pid=$SK_SHELL_PID shell_start=$SK_SHELL_START
  local provider_pid=$SK_PROVIDER_PID provider_start=$SK_PROVIDER_START
  local daemon_pid=$SK_DAEMON_PID daemon_start=$SK_DAEMON_START
  [[ -n $uuid ]] || {
    sk_die "that session has no conversation to carry to another account"
    return 1
  }

  local verdict action reason source_alias target_alias
  verdict=$(python3 "$INVENTORY_CORE" account auto-plan "$provider" "$uuid" \
    --snapshot "$SNAPSHOT" --shpool-id "$id" 2>/dev/null) || {
    sk_die "the account handoff rule could not be evaluated; nothing changed"
    return 1
  }
  action=$(sk_json_field "$verdict" action) || action=""
  reason=$(sk_json_field "$verdict" reason) || reason=""
  source_alias=$(sk_json_field "$verdict" source_alias) || source_alias=""
  target_alias=$(sk_json_field "$verdict" target_alias) || target_alias=""
  if [[ $action != switch ]]; then
    printf '%s\n' "${reason:-No account change was called for.}"
    return 0
  fi
  [[ $target_alias =~ ^[a-z][a-z0-9_-]{0,11}$ &&
     $source_alias =~ ^[a-z][a-z0-9_-]{0,11}$ &&
     $source_alias != "$target_alias" ]] || {
    sk_die "the account handoff named an unusable account; nothing changed"
    return 1
  }
  if [[ $capable != true ]]; then
    printf 'This conversation is on %s, which is spent, but its window cannot change account on its own. Change it from the picker when you are ready.\n' \
      "$source_alias"
    return 0
  fi
  if [[ -n $dry_run ]]; then
    printf 'Would move %s from %s to %s. %s\n' \
      "$(sk_human_label "${title:-}" "$provider" "$number")" \
      "$source_alias" "$target_alias" "$reason"
    return 0
  fi

  account_switch_stable_snapshot "$SNAPSHOT" "$id" "$uuid" "$provider" \
    "$source_alias" || {
    sk_die "the conversation is working; its account was not changed"
    return 1
  }
  account_switch_safe_tree "$provider" "$shell_pid" "$shell_start" \
    "$provider_pid" "$provider_start" || {
    sk_die "something the kit does not recognize is running in that session; its account was not changed"
    return 1
  }
  local switch_error
  switch_error=$(mktemp) || return 1
  python3 "$INVENTORY_CORE" account launch-profile "$provider" "$target_alias" \
    >/dev/null 2>"$switch_error" || {
    sk_die "$(grep -m1 . "$switch_error" 2>/dev/null ||
      echo "the target account could not be prepared; nothing changed")"
    command rm -f -- "$switch_error"
    return 1
  }
  command rm -f -- "$switch_error"

  picker_lock || {
    sk_die "another session action holds the lock; nothing changed"
    return 1
  }
  # Time passed between the first proof and this destructive step, and a new
  # turn can begin in that interval. Prove exact identity, idle state and the
  # recognized process tree again from one fresh snapshot.
  if ! make_guard_snapshot ||
     ! sk_snapshot_matches_exact "$SNAPSHOT" \
       "$id" "$started" "$provider" "$uuid" "$shell_pid" "$shell_start" \
       "$provider_pid" "$provider_start" "$daemon_pid" "$daemon_start" ||
     ! account_switch_stable_snapshot "$SNAPSHOT" "$id" "$uuid" "$provider" \
       "$source_alias" ||
     ! account_switch_safe_tree "$provider" "$shell_pid" "$shell_start" \
       "$provider_pid" "$provider_start"; then
    sk_lock_release 9
    sk_die "the conversation changed while its account handoff was prepared; nothing changed"
    return 1
  fi
  # Preparing the target profile above was a mutating call that ran a provider
  # binary and can take seconds. Ask once more, read-only, whether that account
  # may still be used: the operator can switch an account off at any moment,
  # including inside that window, and their answer outranks the plan's.
  local recheck
  recheck=$(python3 "$INVENTORY_CORE" account auto-target-ok \
    "$provider" "$target_alias" 2>/dev/null) || {
    sk_lock_release 9
    sk_die "$target_alias is no longer available: $(sk_json_field "${recheck:-}" reason ||
      echo "it is not selectable"); nothing changed"
    return 1
  }
  local prepared txid
  prepared=$(python3 "$INVENTORY_CORE" account switch-prepare \
    "$provider" "$uuid" "$source_alias" "$target_alias" "$cwd" "$id" 2>/dev/null) || {
    sk_lock_release 9
    sk_die "the account checkpoint could not be prepared; nothing changed"
    return 1
  }
  txid=$(sk_json_field "$prepared" txid) || txid=
  [[ $txid =~ ^[0-9a-f]{32}$ ]] || {
    sk_lock_release 9
    sk_die "the account checkpoint identity was invalid; nothing changed"
    return 1
  }
  # Reserve the one move BEFORE anything irreversible. Recording it afterwards
  # meant a failure between the account commit and the ledger write left a
  # conversation that had moved with a record saying it never had -- and the
  # next pass then bought it a third account. From here the move is counted
  # whatever happens; only a proven return to the source gives it back.
  local hop_token
  hop_token=$(python3 "$INVENTORY_CORE" account auto-begin "$provider" "$uuid" \
    "$source_alias" "$target_alias" --reason "$reason" 2>/dev/null) || hop_token=
  [[ $hop_token =~ ^[0-9a-f]{32}$ ]] || {
    sk_lock_release 9
    python3 "$INVENTORY_CORE" account switch-rollback "$txid" >/dev/null 2>&1 || true
    sk_die "this move could not be counted against the one-move limit, so it was not made"
    return 1
  }
  if ! account_switch_request "$id" apply "$txid" "$target_alias" "$source_alias" \
       "$uuid" "$started" "$shell_pid" "$shell_start" "$model" "$provider"; then
    sk_lock_release 9
    python3 "$INVENTORY_CORE" account switch-rollback "$txid" >/dev/null 2>&1 || true
    python3 "$INVENTORY_CORE" account auto-release "$provider" "$uuid" \
      "$hop_token" >/dev/null 2>&1 || true
    sk_die "the account handoff request could not be written; nothing changed"
    return 1
  fi
  if ! account_switch_signal "$provider" "$provider_pid" "$provider_start" \
       "$shell_pid" "$shell_start"; then
    command rm -f -- "$SK_STATE_DIR/account-switch-requests/$id"
    sk_lock_release 9
    python3 "$INVENTORY_CORE" account switch-rollback "$txid" >/dev/null 2>&1 || true
    # Nothing was signalled, so the conversation never left its account.
    python3 "$INVENTORY_CORE" account auto-release "$provider" "$uuid" \
      "$hop_token" >/dev/null 2>&1 || true
    sk_die "the exact idle provider changed before handoff; nothing changed"
    return 1
  fi
  sk_lock_release 9
  if account_switch_wait_alias "$id" "$provider" "$uuid" "$target_alias" &&
     python3 "$INVENTORY_CORE" account sync-ui "$provider" "$uuid" \
       "$target_alias" >/dev/null &&
     python3 "$INVENTORY_CORE" account switch-commit "$txid" >/dev/null; then
    sk_log_action account_auto_switch committed || true
    python3 "$INVENTORY_CORE" account auto-commit "$provider" "$uuid" \
      "$hop_token" >/dev/null 2>&1 || true
    # A machine-readable receipt for the periodic driver, which needs the
    # exact identity to record what the operator is owed. Off unless asked
    # for, so a person running this by hand sees one sentence.
    [[ ${SESSION_KIT_AUTO_SWITCH_MARKER:-0} == 1 ]] &&
      printf 'SESSION_KIT_AUTO_SWITCH\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$id" "$provider" "$uuid" "$source_alias" "$target_alias" "$hop_token" >&2
    printf '%s moved from %s to %s. %s The same conversation is still open.\n' \
      "$(sk_human_label "${title:-}" "$provider" "$number")" \
      "$source_alias" "$target_alias" "$reason"
    return 0
  fi
  sk_log_action account_auto_switch rolled_back || true
  # The target did not prove itself. Put the conversation back WITHOUT the
  # destructive last resort: `account_switch_restore_source` ends by killing
  # the session and re-creating it, and a conversation that has to be recovered
  # is not a conversation left working. Unattended, the answer to a failed hop
  # is the non-destructive rollback the managed shell is already waiting for,
  # and a loud refusal if even that cannot be proven.
  if account_switch_restore_source "$id" "$provider" "$uuid" "$source_alias" \
       "$target_alias" "$txid" "$cwd" "$model" --no-recreate; then
    python3 "$INVENTORY_CORE" account auto-release "$provider" "$uuid" \
      "$hop_token" >/dev/null 2>&1 || true
    sk_die "the target account did not prove itself; $source_alias was restored and the conversation is still open"
  else
    # The reservation stands: where the outcome is unproven, refusing a second
    # automatic move costs a manual switch, granting one costs an account.
    sk_die "the target account did not prove itself and $source_alias could not be confirmed; the conversation was left exactly as it is and no further automatic move will be made"
  fi
  return 1
}

teardown_target() {
  # Close one delegated worker and prune the worktree it ran in -- the pair
  # that leaves a merged workstream with nothing of its own left behind. The
  # directory goes only when git agrees the branch is merged and nothing is
  # uncommitted; the branch and its commits are never deleted here.
  local selector= merged_into=HEAD force=
  while (($#)); do
    case "$1" in
      --merged-into)
        [[ -n ${2:-} ]] || { sk_die "--merged-into requires one git ref"; return 2; }
        merged_into=$2
        shift 2
        ;;
      --force)
        force=1
        shift
        ;;
      --*) sk_die "unknown teardown option: $1"; return 2 ;;
      *)
        [[ -z $selector ]] || { sk_die "teardown takes one session"; return 2; }
        selector=$1
        shift
        ;;
    esac
  done
  [[ -n $selector ]] || { sk_die "teardown needs one session"; return 2; }
  resolve_target "$selector" || return $?
  local cwd=$SK_CWD record= branch=
  record=$(python3 "$INVENTORY_CORE" worktree lookup --path "$cwd") || {
    sk_die "that session does not run in a Session Kit worktree; use 'sp close ${SK_NUMBER:-<session>}'"
    return 1
  }
  branch=$(printf '%s' "$record" | python3 -c '
import json,sys
print(json.load(sys.stdin).get("branch") or "")
') || return 1
  [[ -n $branch ]] || {
    sk_die "the worktree record for $cwd names no branch; nothing was closed"
    return 1
  }
  close_target "$selector" || return 1
  # The close releases a copy the kit cut itself, so by here there may be
  # nothing left to prune. That is the wanted outcome, not a failure.
  if ! python3 "$INVENTORY_CORE" worktree lookup --path "$cwd" >/dev/null 2>&1; then
    printf 'Closed the worker and pruned the %s worktree at %s.\n' "$branch" "$cwd"
    return 0
  fi
  local -a teardown_argv=(python3 "$INVENTORY_CORE" worktree teardown
    --path "$cwd" --merged-into "$merged_into")
  [[ -z $force ]] || teardown_argv+=(--force)
  "${teardown_argv[@]}" >/dev/null || {
    sk_die "the worker is closed; its $branch worktree at $cwd was kept for the reason above"
    return 1
  }
  printf 'Closed the worker and pruned the %s worktree at %s.\n' "$branch" "$cwd"
}

show_prune() {
  local candidate_file="$SK_STATE_DIR/prune-candidates.json"
  "$SCRIPT_DIR/shpool_reaper" --candidates >/dev/null || return 1
  # The nomination window is whatever SESSION_KIT_PRUNE_DAYS said when the
  # list was built, and this command inherits that environment. Print the
  # window the list was actually built with, never the default it may not be.
  local days=7
  if [[ -s $candidate_file ]]; then
    days=$(python3 -c 'import json,sys; value=json.load(open(sys.argv[1])).get("max_age_days"); print(value if isinstance(value,int) and not isinstance(value,bool) and value >= 1 else 7)' "$candidate_file" 2>/dev/null) || days=7
  fi
  [[ $days =~ ^[0-9]+$ && $days -ge 1 ]] || days=7
  local window_phrase="$days days"
  if [[ $days == 1 ]]; then
    window_phrase="1 day"
  fi
  if [[ ! -s $candidate_file ]]; then
    echo "Nothing changed."
    return 0
  fi
  local count
  count=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("candidates",[])))' "$candidate_file")
  if (( count == 0 )); then
    echo "Nothing changed."
    return 0
  fi
  printf 'Idle and empty for %s:\n' "$window_phrase"
  if ! python3 - "$candidate_file" <<'PY'
import json,re,sys
rows=json.load(open(sys.argv[1])).get("candidates",[])
pattern=re.compile(r"(?:main(?:[1-9][0-9]*)?|s[0-9]{8}-[0-9]{6}-[1-9][0-9]*(?:-[1-9][0-9]*)?)")
for row in rows:
    value=row.get("shpool_id") if isinstance(row,dict) else None
    if not isinstance(value,str) or len(value.encode())>128 or pattern.fullmatch(value) is None:
        raise SystemExit(2)
for row in rows:
    # The IDs above were validated, not displayed: this list names what is
    # about to close, and a person never types one of these.
    title = " ".join(str(row.get("title") or "").split()) or "empty shell"
    title = "".join(character for character in title if character.isprintable())
    print(f"    {title[:60]}")
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
    sk_die "a prune candidate no longer exists; nothing was pruned"
    return 1
  }
  sk_require_mutation_target || return 1
  # The candidate's empty-tree proof and this mutation guard must describe
  # the same shell generation. The guard document is socket-bound; accepting
  # a namesake shell proved under any other daemon would send the following
  # client kill to the wrong session manager.
  local candidate_shell_pid candidate_shell_start
  read -r candidate_shell_pid candidate_shell_start < <(
    python3 - "$candidate_file" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
pid = value.get("shell_pid") if isinstance(value, dict) else None
start = value.get("shell_start_ticks") if isinstance(value, dict) else None
if (
    not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
    or not isinstance(start, int) or isinstance(start, bool) or start < 0
):
    raise SystemExit(2)
print(pid, start)
PY
  ) || {
    sk_die "prune candidate changed or is no longer empty; no action taken"
    return 1
  }
  if [[ $candidate_shell_pid != "$SK_SHELL_PID" ||
        $candidate_shell_start != "$SK_SHELL_START" ]]; then
    sk_die "prune candidate changed or is no longer empty; no action taken"
    return 1
  fi
  local title=$SK_TITLE provider=$SK_PROVIDER number=${SK_NUMBER:-}
  local candidate_started=$SK_STARTED
  local bound_daemon_pid=$SK_DAEMON_PID bound_daemon_start=$SK_DAEMON_START
  # Nothing is asked. `sp prune` closes a list, so the scripted half of the
  # contract applies one candidate at a time: automation must still name the
  # exact session it is acting on, and a run that names none closes none.
  if [[ ${SESSION_KIT_NONINTERACTIVE:-0} == 1 || ! -t 0 ]] &&
     [[ ${SESSION_KIT_CONFIRM_ID:-} != "$id" ]]; then
    echo "Nothing changed."
    return 1
  fi

  exec 9>"$SK_STATE_DIR/create.lock" || return 1
  sk_lock_acquire 9 "$SK_STATE_DIR/create.lock" || { exec 9>&-; return 1; }
  # This is the final command before exact shell termination. It proves the
  # raw manager generation, disconnected status, stable shell PID/start, and
  # an empty process tree using two fresh snapshots. Any ambiguity fails closed.
  "$SCRIPT_DIR/shpool_reaper" --verify-candidate \
    "$candidate_file" "$SNAPSHOT" >/dev/null || {
    sk_lock_release 9
    sk_die "prune candidate changed or is no longer empty; no action taken"
    return 1
  }
  if ! sk_shpool_holder_is "$bound_daemon_pid" "$bound_daemon_start"; then
    sk_lock_release 9
    sk_die "socket holder changed before closing $id; no kill was sent and nothing was pruned"
    return 1
  fi
  local shell_outcome
  if ! shell_outcome=$(sk_terminate_exact_shell \
      "$bound_daemon_pid" "$bound_daemon_start" \
      "$candidate_shell_pid" "$candidate_shell_start" 2>&1); then
    sk_cleanup_dead_shpool_entry "$id" \
      "$bound_daemon_pid" "$bound_daemon_start" \
      "$candidate_shell_pid" "$candidate_shell_start" "$candidate_started" || true
    sk_lock_release 9
    sk_die "verified shell PID $candidate_shell_pid start $candidate_shell_start survives or could not be terminated ($shell_outcome); nothing was reported closed"
    return 1
  fi
  sk_cleanup_dead_shpool_entry "$id" \
    "$bound_daemon_pid" "$bound_daemon_start" \
    "$candidate_shell_pid" "$candidate_shell_start" "$candidate_started" || {
      sk_lock_release 9
      return 1
    }
  sk_lock_release 9
  # A prune is a person closing sessions, so its conversations join the closed
  # list like any other close rather than vanishing unnamed.
  sk_record_close "$id" "$provider" "${SK_UUID:-}"
  printf 'Closed %s.\n' "$(sk_human_label "$title" "$provider" "$number")"
}

show_history() {
  local selector=$1
  resolve_target "$selector" || return $?
  show_history_id "$SK_ID"
}

sk_page_history() {
  if [[ ${SESSION_KIT_NONINTERACTIVE:-0} == 1 ]]; then
    command cat -- "$@"
  else
    command cat -- "$@" | less -R
  fi
}

# History can also be piped as exact recorded bytes. Keep explanatory copy out
# of that data stream, but retain it on stderr for redirected recall. Callers
# print it after the pager returns so it remains visible on the terminal.
sk_history_notice() {
  printf '%s\n' "$1" >&2
}

# Put an explanation inside the pager while it is open, then repeat it on the
# terminal after the pager exits. With redirected output, recorded bytes stay
# exact on stdout and the explanation appears once on stderr.
sk_page_history_with_notice() {
  local notice=$1
  shift
  if [[ ${SESSION_KIT_NONINTERACTIVE:-0} == 1 || ! -t 1 ]]; then
    command cat -- "$@"
  else
    { printf '%s\n' "$notice"; command cat -- "$@"; } | less -R
  fi
  sk_history_notice "$notice"
}

# Executable overrides exist only to isolate these paths in the test harness.
# Production always executes the tools shipped in its release directory.
sk_history_tool_path() {
  local variable=$1 fallback=$2
  if sk_test_hook "$variable"; then
    printf '%s\n' "$SK_TEST_HOOK"
  else
    printf '%s\n' "$fallback"
  fi
}

# A sidecar is current only when it is readable, nonempty, and no capture that
# feeds it is more than two seconds newer. The grace period absorbs a live
# capture append that races the successful on-demand flush by milliseconds.
# This uses the files already on disk rather than depending on a background
# renderer to invent and maintain a separate failure marker.
sk_history_sidecar_stale() {
  local sidecar=$1 capture sidecar_ns capture_ns
  shift
  [[ -r $sidecar && -s $sidecar ]] || return 0
  sidecar_ns=$(python3 -c \
    'import os, sys; print(os.stat(sys.argv[1]).st_mtime_ns)' \
    "$sidecar" 2>/dev/null) || return 0
  [[ $sidecar_ns =~ ^[0-9]+$ ]] || return 0
  for capture in "$@"; do
    [[ -e $capture ]] || continue
    capture_ns=$(python3 -c \
      'import os, sys; print(os.stat(sys.argv[1]).st_mtime_ns)' \
      "$capture" 2>/dev/null) || return 0
    [[ $capture_ns =~ ^[0-9]+$ ]] || return 0
    (( capture_ns > sidecar_ns + 2000000000 )) && return 0
  done
  return 1
}

show_history_id() {
  local id=$1
  local render_tool transcript_tool
  render_tool=$(sk_history_tool_path SESSION_KIT_JOURNAL_RENDER_TOOL \
    "${INVENTORY_CORE%/*}/sessionkit_inventory/journal_render.py")
  transcript_tool=$(sk_history_tool_path SESSION_KIT_TRANSCRIPT_TEXT_TOOL \
    "${INVENTORY_CORE%/*}/sessionkit_inventory/transcript_text.py")
  local -a files=()
  while IFS= read -r path; do [[ -n $path ]] && files+=("$path"); done < <(history_files "$id")

  if (( ${#files[@]} > 0 )); then
    # Journal exists: replay it through the screen model and page the SETTLED
    # text, never the raw bytes (raw TUI repaints braid when read as text;
    # operator finding 2026-08-12). Incremental: only new bytes render, so a
    # fresh sidecar costs milliseconds and recall stays instant.
    local journal sidecar state
    if [[ -d $SK_JOURNAL_DIR/$id ]]; then
      journal=$SK_JOURNAL_DIR/$id
      sidecar=$journal/rendered.txt
      state=$journal/rendered.state.json
    else
      journal=${files[0]}
      sidecar=${journal%.raw}.rendered.txt
      state=${journal%.raw}.rendered.state.json
    fi
    local render_status=127
    if [[ -f $render_tool ]]; then
      # Serialised against the attach-time refill (sk_replay_history) and any
      # concurrent recall: journal_render.py takes no lock of its own, and two
      # concurrent renders commit the same settled lines twice, permanently.
      # Wait briefly for a holder (an attach refill caps itself at 4s); a
      # longer hold falls through to the stale-sidecar handling below.
      # util-linux flock(1) is deliberately absent on macOS: without it,
      # render unlocked -- the pre-lock behaviour -- rather than turning
      # recall into the raw-capture branch (lane finding F4).
      local -a render_locker=()
      if command -v flock >/dev/null 2>&1; then
        render_locker=(flock -w 10 "$sidecar.lock")
      fi
      ${render_locker[@]+"${render_locker[@]}"} python3 "$render_tool" flush \
        --journal "$journal" --out "$sidecar" --state "$state" >/dev/null 2>&1
      render_status=$?
    fi
    if (( render_status == 0 )) &&
       ! sk_history_sidecar_stale "$sidecar" "${files[@]}"; then
      sk_page_history "$sidecar"
      return 0
    fi
    # A readable but older sidecar is still safer than raw TUI bytes. Its
    # capture timestamp, outside the live-write grace period, is the
    # self-contained evidence that rendering is stale; no fleet-written
    # marker is required.
    if [[ -r $sidecar && -s $sidecar ]]; then
      sk_page_history_with_notice \
        'History rendering needs attention; showing the last readable version.' \
        "$sidecar"
      return 0
    fi
    sk_page_history_with_notice \
      'This session has no clean recording; showing the raw capture.' \
      "${files[@]}"
    return 0
  fi

  # No recording exists (recording was off 2026-07-30..08-12, and sessions
  # created in that window can never be re-recorded). The conversation
  # transcript survives everything, render that instead.
  if [[ ${SK_PROVIDER:-} == claude || ${SK_PROVIDER:-} == codex ]] &&
     [[ -n ${SK_UUID:-} && -f $transcript_tool ]]; then
    local rendered
    rendered=$(command mktemp "${TMPDIR:-/tmp}/sk-transcript.XXXXXX") || rendered=
    if [[ -n $rendered ]] &&
       python3 "$transcript_tool" "$SK_PROVIDER" "$SK_UUID" >"$rendered" 2>/dev/null &&
       [[ -s $rendered ]]; then
      sk_page_history_with_notice \
        'This session was never recorded; showing its conversation instead.' \
        "$rendered"
      command rm -f -- "$rendered"
      return 0
    fi
    [[ -z $rendered ]] || command rm -f -- "$rendered"
  fi
  sk_die "no live history for $(sk_human_label "$SK_TITLE" "$SK_PROVIDER" "${SK_NUMBER:-}")"
  return 1
}

color_target() {
  local selector=$1 action=$2 chosen=${3:-}
  resolve_target "$selector" || return $?
  sk_require_mutation_target || return 1
  [[ $SK_PROVIDER == claude || $SK_PROVIDER == codex ]] || {
    sk_die "a shell session keeps its derived color"
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
      sk_die "could not reset the session color; nothing changed"
      return 1
    }
  else
    python3 "$INVENTORY_CORE" color set "$provider" "$uuid" "$chosen" \
      >/dev/null || {
      sk_lock_release 9
      sk_die "could not set the session color; nothing changed"
      return 1
    }
  fi
  sk_lock_release 9
  # One colour, on every surface that draws this session. The store is
  # updated; a refresh republishes the colour the session's own prompt reads,
  # and the marker plus one safe restart give the provider window the same
  # colour instead of the one it picked for itself at boot. Never on a busy or
  # attached session -- the bounce refuses anything it cannot prove idle.
  make_snapshot >/dev/null 2>&1 || true
  sk_mark_provider_untitled "$id"
  sk_mark_provider_recolor "$id"
  local restarted=0
  SK_PROOF_PROVIDER=$provider SK_PROOF_UUID=$uuid \
    SK_PROOF_PROVIDER_PID=$provider_pid SK_PROOF_PROVIDER_START=$provider_start \
    SK_PROOF_SHELL_PID=$shell_pid SK_PROOF_SHELL_START=$shell_start \
    sk_refresh_provider_title "$id" "$provider" color && restarted=1
  if [[ $action == delete ]]; then
    printf 'Reset the color for %s. It goes back to its derived color.\n' \
      "$(sk_human_label "$SK_TITLE" "$provider" "${SK_NUMBER:-}")"
  else
    printf 'Colored %s %s.\n' \
      "$(sk_human_label "$SK_TITLE" "$provider" "${SK_NUMBER:-}")" "$chosen"
  fi
  # Say which of the two things happened. The line used to promise "from its
  # next start" even when the safe restart two lines up had already applied
  # the colour -- the same kind of copy the reconcile line was just fixed for.
  if (( restarted )); then
    printf 'The %s window restarted and shows it now.\n' \
      "$(sk_provider_name "$provider")"
  else
    printf 'The picker and the prompt show it now; the %s window shows it from its next start.\n' \
      "$(sk_provider_name "$provider")"
  fi
}

# Settle every live same-provider color collision at once, instead of waiting
# for each session to relaunch. Safe to repeat: a second run finds nothing to
# move. No target lock is taken because nothing here depends on one session
# staying put; the core takes the config lock for the write itself.
color_reconcile() {
  local payload
  payload=$(python3 "$INVENTORY_CORE" color reconcile) || {
    sk_die "could not reconcile session colors; nothing changed"
    return 1
  }
  printf '%s' "$payload" | python3 -c '
import json
import sys

result = json.load(sys.stdin)
moved = result.get("moved") or {}
dropped = result.get("dropped") or []
names = {"claude": "Claude", "codex": "Codex"}
for provider in sorted({key.partition(":")[0] for key in moved}):
    colors = sorted(
        color for key, color in moved.items() if key.startswith(provider + ":")
    )
    joined = ", ".join(colors)
    word = "session" if len(colors) == 1 else "sessions"
    print(f"Recolored {len(colors)} {names.get(provider, provider)} {word}: {joined}")
if dropped:
    if len(dropped) == 1:
        print("Dropped 1 stored color that is no longer in the palette.")
    else:
        print(
            f"Dropped {len(dropped)} stored colors that are no longer in the palette."
        )
if not moved and not dropped:
    print("Nothing changed.")
elif moved:
    # Name the providers this run actually recolored. The line used to say
    # "Claude" whatever it had moved, so a person who recolored Codex
    # sessions was told about a provider they had not touched and told
    # nothing about the windows that changed.
    touched = sorted({names.get(key.partition(":")[0], key.partition(":")[0]) for key in moved})
    if len(touched) == 1:
        who = touched[0]
    else:
        who = " and ".join((", ".join(touched[:-1]), touched[-1]))
    print(f"{who} windows show the new color from their next start.")
'
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
  resolve_target "$selector" || return $?
  sk_require_mutation_target || return 1
  [[ $SK_CONVERSATION_PROVIDER == claude || $SK_CONVERSATION_PROVIDER == codex ]] || {
    sk_die "a shell session keeps its derived name"
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
    sk_die "could not update the session name; nothing changed"
    return 1
  }
  sk_lock_release 9
  # One name, on every surface. The alias write above also pushed the name
  # into the provider's own store, but a provider window reads its bar title
  # once, at start: until it restarts it keeps showing the old name, and the
  # kit and the terminal disagree about what this session is called. The
  # marker is the same one a launch leaves when it boots before its name
  # exists, so the safe bounce below -- and the picker's, on the next open --
  # gives the window the name the kit already has. Never on a busy session:
  # the bounce refuses anything that is not proven idle.
  if [[ $action == set ]]; then
    sk_mark_provider_untitled "$id"
    SK_PROOF_PROVIDER=$live_provider SK_PROOF_UUID=$live_uuid \
      SK_PROOF_PROVIDER_PID=$provider_pid SK_PROOF_PROVIDER_START=$provider_start \
      SK_PROOF_SHELL_PID=$shell_pid SK_PROOF_SHELL_START=$shell_start \
      sk_refresh_provider_title "$id" "$live_provider"
  fi
  if [[ $action == delete ]]; then
    printf 'Reset the local name for the %s session.\n' \
      "$(sk_provider_name "$provider")"
  else
    printf 'Named the %s session %s.\n' "$(sk_provider_name "$provider")" \
      "$(sk_human_label "$title" "$provider")"
  fi
}

# The marker that says "this window's bar title is older than the kit's name".
# Advisory: a marker that cannot be written costs a stale bar title until the
# next launch, never the rename.
# The marker that says "this session's colour is newer than its window".
# `sp color` asks for one safe restart immediately, and a session that is busy
# or attached rightly refuses it -- but the person still asked, and without a
# record the request died there: the next open asked only for a NAME bounce,
# which an unnamed session can never satisfy, so the window kept the colour it
# picked for itself while the picker showed the new one. Advisory, like the
# untitled marker: a marker that cannot be written costs a late recolour.
sk_mark_provider_recolor() {
  local id=$1
  ( umask 077
    mkdir -p "$SK_STATE_DIR/provider-recolor" &&
      : > "$SK_STATE_DIR/provider-recolor/$id"
  ) 2>/dev/null || true
}

sk_mark_provider_untitled() {
  local id=$1
  ( umask 077
    mkdir -p "$SK_STATE_DIR/provider-untitled" &&
      printf '%s\n' "$BASHPID:$RANDOM:$EPOCHREALTIME" \
        > "$SK_STATE_DIR/provider-untitled/$id"
  ) 2>/dev/null || true
}

# One safe restart so the name lands on the bar now rather than at the next
# open. Every guard inside the bounce fails toward doing nothing, and an
# attached or working session is never restarted under the person using it.
# Returns 0 when the window really was restarted, so the caller can say what
# happened rather than always promising "from its next start".
sk_refresh_provider_title() {
  local id=$1 provider=$2 reason=${3:-name}
  case "$provider" in
    claude) picker_bounce_claude "$id" automatic "$reason" >/dev/null 2>&1 ;;
    codex) picker_bounce_codex "$id" automatic "$reason" >/dev/null 2>&1 ;;
    *) return 1 ;;
  esac
}

find_history() {
  local query=$1
  [[ -n $query ]] || return 1
  local search_tool
  search_tool=$(sk_history_tool_path SESSION_KIT_HISTORY_SEARCH_TOOL \
    "${INVENTORY_CORE%/*}/sessionkit_inventory/history_search.py")
  [[ -f $search_tool ]] || {
    sk_die "history search is unavailable"
    return 1
  }
  local inventory="${SESSION_KIT_HISTORY_INVENTORY:-$SK_STATE_DIR/inventory.json}"
  python3 "$search_tool" --journal "$SK_JOURNAL_DIR" --recovery "$SK_RECOVERY_DIR" \
    --archive "$SK_ARCHIVE_DIR" --inventory "$inventory" \
    --data "${SESSION_KIT_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/session-kit}" \
    -- "$query"
}

# Every number on this screen is labeled. The unlabeled `ps` columns it used to
# print named nothing: no reader could tell which of five figures was memory.
show_health() {
  sk_require_shpool || return 1
  local daemon
  daemon=$(pgrep -f '^.*/shpool daemon$' | head -1)
  if [[ -z $daemon ]]; then
    sk_die "the session manager is not running"
    return 1
  fi
  local rss threads elapsed
  rss=$(ps -o rss= -p "$daemon" 2>/dev/null | tr -d ' ')
  threads=$(ps -o nlwp= -p "$daemon" 2>/dev/null | tr -d ' ')
  elapsed=$(ps -o etime= -p "$daemon" 2>/dev/null | tr -d ' ')
  printf '  Session manager\n\n'
  printf '  Running     %s\n' "$(sk_elapsed_phrase "$elapsed")"
  printf '  Memory      %s\n' "$(sk_memory_phrase "$rss")"
  [[ -z $threads ]] || printf '  Threads     %s\n' "$threads"
  local host_memory
  host_memory=$(free -b 2>/dev/null | awk '
    $1 == "Mem:" { printf "%.0f %.0f", $7, $2 }
  ')
  if [[ -n $host_memory ]]; then
    local host_available host_total
    read -r host_available host_total <<<"$host_memory"
    printf '\n  Host memory %s available of %s\n' \
      "$(sk_bytes_phrase "$host_available")" "$(sk_bytes_phrase "$host_total")"
  fi
}

# ps prints etime as [[dd-]hh:]mm:ss. Say it the way a person would.
sk_elapsed_phrase() {
  local raw=${1:-}
  [[ -n $raw ]] || { printf '—'; return 0; }
  local days=0 rest=$raw
  if [[ $rest == *-* ]]; then
    days=${rest%%-*}
    rest=${rest#*-}
  fi
  local hours=0
  case "$rest" in
    *:*:*) hours=${rest%%:*} ;;
  esac
  days=$((10#${days:-0}))
  hours=$((10#${hours:-0}))
  if (( days == 1 )); then
    printf '1 day'
  elif (( days > 1 )); then
    printf '%s days' "$days"
  elif (( hours == 1 )); then
    printf '1 hour'
  elif (( hours > 1 )); then
    printf '%s hours' "$hours"
  else
    printf 'less than an hour'
  fi
}

sk_memory_phrase() {
  local kilobytes=${1:-}
  [[ $kilobytes =~ ^[0-9]+$ ]] || { printf '—'; return 0; }
  sk_bytes_phrase "$(( kilobytes * 1024 ))"
}

sk_bytes_phrase() {
  local bytes=${1:-}
  [[ $bytes =~ ^[0-9]+$ ]] || { printf '—'; return 0; }
  awk -v bytes="$bytes" '
    BEGIN {
      if (bytes >= 1073741824) { printf "%.1f GB", bytes / 1073741824 }
      else if (bytes >= 1048576) { printf "%.1f MB", bytes / 1048576 }
      else { printf "%.0f KB", bytes / 1024 }
    }'
}

# One feed, newest first: the conversations a person closed on purpose and the
# ones a crash took. Closing a session ends the terminal, never the
# conversation, so a deliberate close is listed here exactly like a lost one --
# hiding it was the broken half of the promise. A plain shell has no
# conversation to reopen and says so rather than offering a restore that
# cannot work.
sk_recovery_feed() {
  local allow_large=${1:-no}
  local snapshot_file
  snapshot_file=$(mktemp "${TMPDIR:-/tmp}/session-kit-recovery-snapshot.XXXXXX") || {
    sk_die "the recovery list could not create its private snapshot file"
    return 1
  }
  chmod 600 "$snapshot_file"
  # One core invocation validates and snapshots the Closed ledger once. The
  # first line carries pending projection inputs whose tombstone decisions use
  # that snapshot; all following lines are Closed rows from the same reusable
  # SQLite snapshot. A concurrent forget is therefore wholly before or wholly
  # after the rendered view, never between two authority reads.
  local -a snapshot_args=(recovery-pending list --stream-recovery-snapshot)
  [[ $allow_large != yes ]] || snapshot_args+=(--allow-large-ledger)
  if ! python3 "$INVENTORY_CORE" "${snapshot_args[@]}" >"$snapshot_file"; then
    rm -f -- "$snapshot_file"
    printf 'session-kit: the closed-conversations list could not be read, so anything closed on purpose is missing from this feed.\n' >&2
    return 1
  fi
  python3 /dev/fd/3 3<<'FEED_PY' <"$snapshot_file"
import datetime, json, os, re, sqlite3, sys, tempfile, unicodedata

first = sys.stdin.readline()
if not first:
    raise ValueError("recovery snapshot stream is empty")
envelope = json.loads(first)
base = envelope.get("recovery_projection") if isinstance(envelope, dict) else None
if not isinstance(base, dict):
    raise ValueError("recovery snapshot stream has no projection header")
for diagnostic in base.get("diagnostics") or ():
    if isinstance(diagnostic, str) and diagnostic:
        print(f"session-kit: {diagnostic}", file=sys.stderr)
inputs = base.get("projection_inputs") or {}
aliases = inputs.get("aliases") if isinstance(inputs.get("aliases"), dict) else {}
automatic = inputs.get("automatic_titles") if isinstance(inputs.get("automatic_titles"), dict) else {}
raw_numbers = inputs.get("numbers") if isinstance(inputs.get("numbers"), dict) else {}
counts = {}
for value in raw_numbers.values():
    counts[value] = counts.get(value, 0) + 1
numbers = {key: value for key, value in raw_numbers.items() if counts.get(value) == 1}
live = {
    (str(pair[0]), str(pair[1]).casefold())
    for pair in (inputs.get("live") or [])
    if isinstance(pair, list) and len(pair) == 2 and pair[0] and pair[1]
}
uuid_shape = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}")
generated = (
    re.compile(r"the(\s+\w+)?\s+session"), re.compile(r"idle shell"),
    re.compile(r"(claude|codex|shell)"),
    re.compile(r"(claude|codex|shell)\s+(in\s+\S+\s+at|started)\s+.+"),
    re.compile(r"[a-z0-9][a-z0-9._-]*-[0-9a-f]{2,4}"),
)

def clean(value, limit):
    if not isinstance(value, str):
        return ""
    safe = "".join(" " if unicodedata.category(c).startswith("C") else c for c in value)
    return " ".join(safe.split())[:limit]

def exact_uuid(value):
    return value.lower() if isinstance(value, str) and uuid_shape.fullmatch(value) else ""

def real_name(row, provider, exact):
    if provider == "shell":
        return ""
    key = f"{provider}:{exact}"
    for store in (aliases, automatic):
        found = clean(store.get(key), 120)
        if found:
            return found
    title = clean(row.get("title"), 120)
    source = clean(row.get("title_source"), 40).casefold()
    if source:
        return "" if source in {"context", "provider"} else title
    folded = title.casefold()
    return "" if any(shape.fullmatch(folded) for shape in generated) else title

def closed_candidate(row):
    provider = clean(row.get("provider"), 20).casefold()
    exact = exact_uuid(row.get("uuid"))
    if exact and (provider, exact.casefold()) in live:
        return None
    name = real_name(row, provider, exact)
    number = numbers.get(f"ai:{provider}:{exact}")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        number = None
    restorable = bool(row.get("restorable")) and provider != "shell" and bool(exact)
    return {
        "source": "closed", "number": number, "short_id": exact[:8],
        "provider": provider, "uuid": exact, "cwd": clean(row.get("cwd"), 4096),
        "name": name, "named": bool(name), "display_name": name or "unnamed",
        "restorable": restorable,
        "history_only_reason": "" if restorable else (
            "a plain shell, so only its history is kept -- there is no conversation to reopen"
            if provider == "shell" or not exact else
            "its transcript is no longer on this machine, so only its history is kept"
        ),
        "when_unix_ms": row.get("closed_at_unix_ms") if isinstance(row.get("closed_at_unix_ms"), int) else 0,
        "source_generation_key": "", "old_shpool_id": clean(row.get("shpool_id"), 200),
        "conflict_fields": [], "selector": "",
    }

descriptor, database = tempfile.mkstemp(prefix="session-kit-recovery-feed-", suffix=".sqlite3")
os.close(descriptor)
os.chmod(database, 0o600)
connection = sqlite3.connect(database)
try:
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute("PRAGMA temp_store=FILE")
    connection.execute("PRAGMA cache_size=-2048")
    connection.execute("""CREATE TABLE rows (
        identity TEXT PRIMARY KEY, provider TEXT, uuid TEXT, when_ms INTEGER,
        number INTEGER, display_name TEXT, name_fold TEXT, named INTEGER,
        restorable INTEGER, selector TEXT, document TEXT
    ) WITHOUT ROWID""")

    def identity(row):
        if row.get("uuid"):
            return f"ai:{row.get('provider')}:{str(row.get('uuid')).casefold()}"
        old = clean(row.get("old_shpool_id"), 200)
        return f"shell:{old or clean(row.get('cwd'), 4096)}:{row.get('when_unix_ms') or 0}"

    def retain(row):
        if not isinstance(row, dict):
            raise ValueError("recovery stream contains a non-object")
        row = dict(row)
        row["selector"] = ""
        key = identity(row)
        held_raw = connection.execute("SELECT document FROM rows WHERE identity=?", (key,)).fetchone()
        if held_raw is not None:
            held = json.loads(held_raw[0])
            if (row.get("when_unix_ms") or 0) > (held.get("when_unix_ms") or 0):
                for field in ("source", "when_unix_ms", "cwd", "restorable", "history_only_reason"):
                    held[field] = row.get(field)
                if row.get("named"):
                    for field in ("name", "named", "display_name"):
                        held[field] = row.get(field)
            elif row.get("named") and not held.get("named"):
                for field in ("name", "named", "display_name"):
                    held[field] = row.get(field)
            for field in ("source_generation_key", "old_shpool_id", "conflict_fields"):
                if row.get(field) and not held.get(field):
                    held[field] = row[field]
            row = held
        name = clean(row.get("display_name"), 120) or "unnamed"
        number = row.get("number") if isinstance(row.get("number"), int) and not isinstance(row.get("number"), bool) else None
        connection.execute(
            "INSERT OR REPLACE INTO rows VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (key, row.get("provider") or "", row.get("uuid") or "", row.get("when_unix_ms") or 0,
             number, name, name.casefold(), 1 if row.get("named") else 0,
             1 if row.get("restorable") else 0, "", json.dumps(row, ensure_ascii=False, sort_keys=True)),
        )

    for row in base.get("entries") or []:
        retain(row)
    for line_number, raw in enumerate(sys.stdin, 2):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"closed-session stream row {line_number} is invalid JSON") from exc
        candidate = closed_candidate(record)
        if candidate is not None:
            retain(candidate)
    connection.commit()

    # Main's cap bounds history-only reading but never restorable work.
    restorable_count = connection.execute("SELECT count(*) FROM rows WHERE restorable=1").fetchone()[0]
    history_keep = max(0, 500 - restorable_count)
    connection.execute(
        """DELETE FROM rows WHERE restorable=0 AND identity NOT IN
           (SELECT identity FROM rows WHERE restorable=0
            ORDER BY when_ms DESC, coalesce(number, 2147483647), uuid LIMIT ?)""",
        (history_keep,),
    )
    connection.execute("CREATE TABLE tokens (folded TEXT PRIMARY KEY) WITHOUT ROWID")
    connection.execute("UPDATE rows SET selector=CAST(number AS TEXT) WHERE number IS NOT NULL")
    connection.execute(
        "INSERT OR IGNORE INTO tokens SELECT CAST(number AS TEXT) FROM rows WHERE number IS NOT NULL"
    )

    connection.create_function("fold", 1, lambda value: str(value or "").casefold())
    connection.create_function(
        "stamp", 2,
        lambda when, pattern: "" if not when or when <= 0 else "@" + datetime.datetime.fromtimestamp(when / 1000).strftime(pattern),
    )
    connection.execute("CREATE TABLE claims (folded TEXT PRIMARY KEY, token TEXT, claims INTEGER) WITHOUT ROWID")

    def offer(expression, parameters=()):
        connection.execute("DELETE FROM claims")
        connection.execute(
            f"""INSERT INTO claims
                SELECT fold({expression}), min({expression}), count(*) FROM rows
                WHERE selector='' AND restorable=1 AND {expression}!=''
                GROUP BY fold({expression})""",
            parameters * 4,
        )
        connection.execute("DELETE FROM claims WHERE claims!=1 OR folded IN (SELECT folded FROM tokens)")
        connection.execute(
            f"""UPDATE rows SET selector=(SELECT token FROM claims WHERE claims.folded=fold({expression}))
                WHERE selector='' AND restorable=1 AND fold({expression}) IN (SELECT folded FROM claims)""",
            parameters * 2,
        )
        connection.execute("INSERT OR IGNORE INTO tokens SELECT folded FROM claims")

    offer("display_name")
    offer("stamp(when_ms, ?)", ("%H:%M",))
    offer("stamp(when_ms, ?)", ("%H:%M:%S",))
    connection.commit()
    for selector, document in connection.execute(
        "SELECT selector, document FROM rows ORDER BY when_ms DESC, coalesce(number, 2147483647), uuid"
    ):
        row = json.loads(document)
        row["selector"] = selector
        print(json.dumps(row, ensure_ascii=False, sort_keys=True))
finally:
    connection.close()
    try:
        os.unlink(database)
    except FileNotFoundError:
        pass
FEED_PY
  local feed_status=$?
  rm -f -- "$snapshot_file"
  if (( feed_status != 0 )); then
    printf 'session-kit: the recovery list could not be rendered; no empty result should be trusted.\n' >&2
  fi
  return "$feed_status"
}

sk_render_recovery_feed() {
  python3 /dev/fd/3 3<<'RENDER_PY'
import json, sys, time
now_ms = int(time.time() * 1000)
def age(when):
    if not isinstance(when, int) or isinstance(when, bool) or when <= 0:
        return "age unavailable"
    seconds = max(0, (now_ms - when) // 1000)
    if seconds < 60: return "under 1 min ago"
    if seconds < 3600: return f"{seconds // 60} min ago"
    if seconds < 86400: return f"{seconds // 3600} hr ago"
    days = seconds // 86400
    return f"{days} day{'s' if days != 1 else ''} ago"
for raw in sys.stdin:
    if not raw.strip():
        continue
    row = json.loads(raw)
    number = row.get("number")
    print("\t".join((
        str(number) if isinstance(number, int) and not isinstance(number, bool) else "",
        " ".join(str(row.get("selector") or "").split()), row.get("source") or "",
        row.get("provider") or "", row.get("uuid") or "", row.get("cwd") or "",
        "yes" if row.get("restorable") else "no", age(row.get("when_unix_ms")),
        " ".join(str(row.get("display_name") or "").split()),
        " ".join(str(row.get("history_only_reason") or "").split()),
    )))
RENDER_PY
}

show_recovery() {
  local allow_large=${1:-no}
  local feed_file status line
  feed_file=$(mktemp "${TMPDIR:-/tmp}/session-kit-recovery.XXXXXX") || {
    sk_die "the recovery list could not create its private stream file"
    return 1
  }
  chmod 600 "$feed_file"
  sk_recovery_feed "$allow_large" >"$feed_file"
  status=$?
  if (( status != 0 )); then
    rm -f -- "$feed_file"
    return "$status"
  fi
  if [[ ! -s $feed_file ]]; then
    rm -f -- "$feed_file"
    echo "Closed conversations: none."
    return 0
  fi
  # What this screen prints beside each row, written down before it is
  # printed. `sp restore` rebuilds the list to act on it, so without this it
  # has no way to tell that the word it was handed has changed rows since they
  # read it -- and two rows can carry the same name, so the success line would
  # not tell them either.
  local recorded=1
  python3 "$INVENTORY_CORE" recovery-selectors remember --json-lines \
    <"$feed_file" >/dev/null || recorded=0
  local number selector source provider uuid cwd restorable event_age title reason
  while IFS= read -r line; do
    # A tab is IFS whitespace, so a run of them reads as one separator and an
    # empty field disappears -- and a shell row's conversation field is always
    # empty. Every record here is read on \034 for that reason.
    line=${line//$'\t'/$'\034'}
    IFS=$'\034' read -r number selector source provider uuid cwd restorable event_age title reason <<<"$line"
    local note="" label="—"
    [[ $source != lost ]] || note=" — lost with its session"
    # The number a session has everywhere else, not a position in this list.
    # A conversation whose number was retired goes by the name beside it, or
    # by the time of its own event when two rows answer to one name -- and the
    # picker prints the same thing in the same column. Neither is a session
    # identifier, which is what this column used to print.
    if [[ -n $number ]]; then
      label=$number
    elif [[ $selector == @* ]]; then
      label=$selector
    fi
    printf '%4s  %-6s  %s · %s%s\n' "$label" "$(sk_provider_name "$provider")" \
      "$title" "$event_age" "$note"
    [[ -z $reason ]] || printf '        %s\n' "$reason"
  done < <(sk_render_recovery_feed <"$feed_file")
  rm -f -- "$feed_file"
  if [[ $allow_large == yes ]]; then
    printf 'Bring one back with: sp restore --allow-large-ledger number|selector, using what this list shows.\n'
  else
    printf 'Bring one back with: sp restore <number>, or the selector beside a session that has none.\n'
  fi
  (( recorded )) || printf 'session-kit: this list could not be written down, so sp restore cannot check what you type against it\n' >&2
}

# The same feed, acted on. The number is the session's own number, so it names
# the same conversation on every screen and does not move when the list is
# rebuilt between printing it and acting on it.
restore_listed() {
  local wanted=$1
  local allow_large=${2:-no}
  # Everything after the verb is one selector; normalize it exactly as the
  # printed-selector record does, including a multi-word unquoted name.
  wanted=$(printf '%s' "$wanted" | tr -s '[:space:]' ' ')
  wanted=${wanted# }
  wanted=${wanted% }
  [[ -n $wanted ]] || {
    sk_die "restore takes a session number from sp recover, or the selector beside a session that has none"
    return 2
  }
  local feed_file status line=""
  feed_file=$(mktemp "${TMPDIR:-/tmp}/session-kit-restore.XXXXXX") || {
    sk_die "the recovery list could not create its private stream file"
    return 1
  }
  chmod 600 "$feed_file"
  sk_recovery_feed "$allow_large" >"$feed_file"
  status=$?
  if (( status != 0 )); then
    rm -f -- "$feed_file"
    return "$status"
  fi
  # A session number, or -- for a conversation whose number was retired -- the
  # selector the list prints beside it, which is the name unless two rows
  # answer to one name. The list settles that once, for both screens, so this
  # cannot resolve a word the picker would resolve differently. Neither is a
  # position, and neither is an identifier: the short id this used to accept
  # was never printed anywhere, so the only way to learn it was to read the
  # JSON.
  wanted=$(printf '%s' "$wanted" | tr -s '[:space:]' ' ')
  wanted=${wanted# }
  wanted=${wanted% }
  [[ -n $wanted ]] || {
    sk_die "restore takes a session number from sp recover, or the selector beside a session that has none"
    return 2
  }
  local folded=${wanted,,}
  local number selector source provider uuid cwd restorable event_age title reason
  local found=0 line
  local match_number="" match_source="" match_provider="" match_uuid="" match_cwd=""
  local match_restorable="" match_title="" match_reason=""
  while IFS= read -r line; do
    [[ -n $line ]] || continue
    line=${line//$'\t'/$'\034'}
    IFS=$'\034' read -r number selector source provider uuid cwd restorable event_age title reason <<<"$line"
    # One rule for every word, which is the rule the picker applies: the word
    # answers for the row it is printed beside. A numbered row's selector IS
    # its number, so a number needs no branch of its own -- and a branch of its
    # own is how a conversation somebody named "2 4" ended up meaning sessions
    # 2 and 4 on one screen and itself on the other.
    [[ -n $selector && ${selector,,} == "$folded" ]] || continue
    (( found == 0 )) || {
      # Two rows answering to one selector is a broken list; restoring the
      # first of them is how a person gets back a conversation they did not ask
      # for.
      sk_die "more than one closed session answers to that, so nothing was restored"
      return 2
    }
    found=1
    match_number=$number
    match_source=$source
    match_provider=$provider
    match_uuid=$uuid
    match_cwd=$cwd
    match_restorable=$restorable
    match_title=$title
    match_reason=$reason
  done < <(sk_render_recovery_feed <"$feed_file")
  rm -f -- "$feed_file"
  (( found )) || {
    sk_die "there is no session ${wanted} on that list. Type what the row shows: its number, or the selector beside a session that has none"
    return 2
  }
  number=$match_number
  source=$match_source
  provider=$match_provider
  uuid=$match_uuid
  cwd=$match_cwd
  restorable=$match_restorable
  title=$match_title
  reason=$match_reason
  # The list was rebuilt to answer this, and a word like "unnamed" -- or the
  # clock face a shared name falls back to -- belongs to the list rather than
  # to the conversation. So before anything is restored: does this word still
  # name the row it was printed beside? A word they have never been shown has no
  # earlier meaning to have drifted from, and is left alone.
  local verdict=0
  python3 "$INVENTORY_CORE" recovery-selectors check "$wanted" "$provider" "$uuid" \
    >/dev/null 2>&1 || verdict=$?
  if (( verdict == 4 )); then
    sk_die "${wanted} was printed beside a different conversation, so nothing was restored. Run sp recover and type what the list shows now"
    return 1
  elif (( verdict != 0 )); then
    sk_die "the kit could not check that ${wanted} still means the conversation it was printed beside, so nothing was restored"
    return 1
  fi
  [[ $restorable == yes ]] || {
    sk_die "${reason:-that one keeps its history only; there is no conversation to restore}"
    return 1
  }
  [[ -d $cwd ]] || {
    sk_die "the directory that session ran in is gone, so it cannot be restored"
    return 1
  }
  restore_exact "$provider" "$uuid" "$cwd" >/dev/null || return 1
  printf 'Restored %s.\n' "$title"
}

verify_start_target() {
  local selector=$1
  sk_require_integration || return 1
  resolve_target "$selector" || return $?
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
    printf 'Verified %s started and cleared the retained launch record for %s.\n' \
      "$(sk_provider_name "$expected_provider")" \
      "$(sk_human_label "$SK_TITLE" "$expected_provider" "${SK_NUMBER:-}")"
    return 0
  fi
  sk_die "$(sk_provider_name "$expected_provider") is not running in that session yet; its launch record is kept. Open it from the picker and try again."
}
