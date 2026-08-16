# shellcheck shell=bash
# ---- session-kit shpool integration -----------------------------------------
# Source this block from ~/.bashrc. Installed releases keep this file immutable.

if [[ ${__SESSION_KIT_SHPOOL_BASHRC_LOADED:-0} == 1 ]]; then
  return 0
fi
__SESSION_KIT_SHPOOL_BASHRC_LOADED=1

export PATH="$HOME/.cargo/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

# Keep AI TUI output in normal terminal history. The session journal is the
# reconnect source; provider-native transcripts remain the conversation source.
export CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1

__sk_state_root=${XDG_STATE_HOME:-"$HOME/.local/state"}
__sk_kit_state=${SESSION_KIT_STATE_DIR:-"$__sk_state_root/session-kit"}
__sk_journal_root=${SESSION_KIT_JOURNAL_DIR:-"$__sk_state_root/shpool-journal"}


# A disabled journal is still a launch-ready shell. Mark it explicitly so the
# one-shot provider record below is consumed without starting `script`.
if [[ -n ${SHPOOL_SESSION_NAME:-} && -z ${SHPOOL_JOURNAL:-} && $- == *i* \
      && -e $HOME/.no_shpool_journal ]]; then
  export SHPOOL_JOURNAL=disabled
fi

# New sessions write one append-only segment for their entire live lifetime.
# Active segments are never replaced or trimmed. Existing legacy sessions keep
# their already-open writer and are handled by the recovery map.
if [[ -n ${SHPOOL_SESSION_NAME:-} && -z ${SHPOOL_JOURNAL:-} && $- == *i* && -t 1 \
      && ! -e $HOME/.no_shpool_journal ]]; then
  __sk_session_journal="$__sk_journal_root/$SHPOOL_SESSION_NAME"
  __sk_journal_ready=1
  mkdir -p "$__sk_session_journal" || __sk_journal_ready=0
  if (( __sk_journal_ready )); then
    chmod 700 "$__sk_session_journal" || __sk_journal_ready=0
  fi
  if (( __sk_journal_ready )); then
    export SHPOOL_JOURNAL="$__sk_session_journal/segment-000001.raw"
    ( umask 077; : >> "$SHPOOL_JOURNAL" ) || __sk_journal_ready=0
    chmod 600 "$SHPOOL_JOURNAL" || __sk_journal_ready=0
  fi
  if (( ! __sk_journal_ready )); then
    echo "[session-kit: journal unavailable; continuing without capture]" >&2
    export SHPOOL_JOURNAL=disabled
    exec bash -i
  fi
  if [[ $(uname -s 2>/dev/null) == Darwin ]]; then
    script -q -F -a "$SHPOOL_JOURNAL" bash -i
  else
    script -qfa "$SHPOOL_JOURNAL" -c "bash -i"
  fi
  __sk_script_rc=$?
  if (( __sk_script_rc != 0 )); then
    echo "[session-kit: journal process failed; continuing without capture]" >&2
    export SHPOOL_JOURNAL=disabled
    exec bash -i
  fi
  unset __sk_script_rc __sk_journal_ready
  builtin exit
fi

if [[ -n ${SHPOOL_SESSION_NAME:-} && $- == *i* ]]; then
  # A refusal that only reaches this session's screen is invisible the moment
  # the session is closed. Three launches started nothing one night and the
  # only way to know WHY was to attach before somebody closed them. Every
  # decision this block makes now lands on the action log, named by session.
  __sk_log_launch() {
    local __sk_log_core=${SESSION_KIT_INVENTORY_CORE:-${__sk_inventory_core:-}}
    if [[ -z $__sk_log_core ]]; then
      # The earliest refusals happen before the record block resolves the
      # release, and those are exactly the ones nobody could diagnose. Derive
      # it the same way that block does, from this file's own location.
      local __sk_log_root
      __sk_log_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd -P) ||
        __sk_log_root=""
      [[ -z $__sk_log_root ]] || __sk_log_core=$__sk_log_root/lib/session_inventory.py
    fi
    [[ -n $__sk_log_core && -f $__sk_log_core ]] || return 0
    python3 "$__sk_log_core" action-log launch "$1" \
      --session "$SHPOOL_SESSION_NAME" >/dev/null 2>&1 || true
  }
  __sk_start_dir=${SESSION_KIT_START_DIR:-"$__sk_state_root/shpool-start"}
  __sk_start="$__sk_start_dir/$SHPOOL_SESSION_NAME"
  __sk_expected="$__sk_start.expected"
  __sk_launch="$__sk_start.launch"
  __sk_account="$__sk_start.account"
  # The shell can start before `sp` has WRITTEN the main record at all — the
  # session exists the moment shpool spawns it, and on a loaded box the
  # launcher's first write can land a second later. A one-shot existence
  # check here made that launch permanently silent. The wait is bounded so a
  # session with no launcher (a bare shpool attach) costs three seconds once.
  if [[ -n ${SHPOOL_JOURNAL:-} && ! -r $__sk_start ]]; then
    for __sk_arm_attempt in {1..30}; do
      [[ -r $__sk_start ]] && break
      sleep 0.1
    done
  fi
  if [[ -n ${SHPOOL_JOURNAL:-} && -r $__sk_start ]]; then
    # The shell can start before `sp` has captured its exact generation. Wait
    # only for an atomically written sidecar; an unarmed or stale main record
    # never launches a provider.
    # 3 seconds covers a healthy launch; a loaded box can take longer to arm
    # (a lost-by-milliseconds launch once left a relaunch hanging on a
    # plain shell). At the 3-second mark, a FRESH main record proves a launch
    # is still in progress, and only that case earns the longer wait — a
    # stale record still costs later shells 3 seconds, not 30.
    for __sk_arm_attempt in {1..300}; do
      [[ -r $__sk_expected ]] && break
      if (( __sk_arm_attempt == 30 )); then
        __sk_arm_started=$(stat -c %Y "$__sk_start" 2>/dev/null ||
          stat -f %m "$__sk_start" 2>/dev/null || echo 0)
        (( $(date +%s) - __sk_arm_started < 60 )) || break
      fi
      sleep 0.1
    done
    unset __sk_arm_started
    # An exhausted wait means sp died between writing the start record and
    # arming it. Every later shell in this session would silently stall 3s
    # here; say it once instead.
    #
    # Except for a shell session, where a plain shell IS what was asked for:
    # `sp new shell` arms its record and then clears it once the shell is
    # open, so a missing sidecar there is the ordinary end of a healthy
    # launch. Warning about it called a working session broken.
    if [[ ! -r $__sk_expected ]]; then
      __sk_start_provider=
      if [[ -r $__sk_start ]]; then
        IFS=$'\t' read -r __sk_start_provider __sk_start_rest < "$__sk_start" ||
          __sk_start_provider=
      fi
      if [[ -r $__sk_start && $__sk_start_provider != shell ]]; then
        echo "[session-kit: launch record incomplete; starting a plain shell]"
        __sk_log_launch refused_record_incomplete
      fi
      unset __sk_start_provider __sk_start_rest
    fi
  fi
  if [[ -n ${SHPOOL_JOURNAL:-} && -r $__sk_start && -r $__sk_expected ]]; then
    __sk_provider= __sk_cwd= __sk_uuid= __sk_launch_mode=
    __sk_side_provider= __sk_side_cwd= __sk_side_uuid= __sk_side_launch_mode=
    __sk_boot_id= __sk_started= __sk_shell_pid= __sk_shell_start=
    __sk_daemon_pid= __sk_daemon_start=
    __sk_requested_model= __sk_launch_key=
    __sk_launch_provider= __sk_launch_cwd= __sk_launch_boot=
    __sk_launch_started= __sk_launch_shell_pid= __sk_launch_shell_start=
    __sk_launch_daemon_pid= __sk_launch_daemon_start=
    __sk_launch_record_ok=1
    __sk_record_shape_ok=0
    if python3 - "$__sk_start" "$__sk_expected" <<'PY'
import sys

expected_tabs = ({2, 3}, {8, 9})
for path, allowed in zip(sys.argv[1:], expected_tabs):
    with open(path, "rb") as handle:
        payload = handle.read(8193)
    if (
        not payload
        or len(payload) > 8192
        or not payload.endswith(b"\n")
        or payload.count(b"\n") != 1
        or b"\r" in payload
        or b"\x1c" in payload
        or payload[:-1].count(b"\t") not in allowed
    ):
        raise SystemExit(1)
PY
    then
      IFS= read -r __sk_start_line < "$__sk_start"
      IFS= read -r __sk_expected_line < "$__sk_expected"
      __sk_start_line=${__sk_start_line//$'\t'/$'\034'}
      __sk_expected_line=${__sk_expected_line//$'\t'/$'\034'}
      IFS=$'\034' read -r __sk_provider __sk_cwd __sk_uuid __sk_launch_mode <<<"$__sk_start_line"
      IFS=$'\034' read -r __sk_side_provider __sk_side_cwd \
        __sk_boot_id __sk_started __sk_shell_pid __sk_shell_start \
        __sk_daemon_pid __sk_daemon_start __sk_side_uuid __sk_side_launch_mode <<<"$__sk_expected_line"
      __sk_record_shape_ok=1
    fi
    if [[ -z $__sk_launch_mode ]]; then
      if [[ -n $__sk_uuid ]]; then __sk_launch_mode=resume; else __sk_launch_mode=new; fi
    fi
    if [[ -z $__sk_side_launch_mode ]]; then
      if [[ -n $__sk_side_uuid ]]; then __sk_side_launch_mode=resume; else __sk_side_launch_mode=new; fi
    fi
    # Session shells are spawned by the shpool daemon and carry NO kit
    # environment, so the release must be derived from this file's own
    # location (resolving the `current` link pins the release active at
    # session start). The env overrides remain for tests and tooling.
    __sk_release_root=${SESSION_KIT_RELEASE_DIR:-}
    if [[ -z $__sk_release_root ]]; then
      __sk_release_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd -P) ||
        __sk_release_root=""
    fi
    __sk_inventory_core=${SESSION_KIT_INVENTORY_CORE:-$__sk_release_root/lib/session_inventory.py}
    unset __sk_release_root
    if [[ -e $__sk_launch || -L $__sk_launch ]]; then
      __sk_launch_record_ok=0
      if [[ -f $__sk_launch && ! -L $__sk_launch ]] &&
         python3 - "$__sk_launch" <<'PY'
import os
import stat
import sys
path = sys.argv[1]
info = os.lstat(path)
with open(path, "rb") as stream:
    payload = stream.read(8193)
if (
    not stat.S_ISREG(info.st_mode)
    or info.st_uid != os.geteuid()
    or stat.S_IMODE(info.st_mode) != 0o600
    or not payload
    or len(payload) > 8192
    or not payload.endswith(b"\n")
    or payload.count(b"\n") != 1
    or payload[:-1].count(b"\t") != 9
    or b"\r" in payload
    or b"\x1c" in payload
):
    raise SystemExit(1)
PY
      then
        # TAB IS IFS WHITESPACE, so `IFS=$'\t' read` collapses a run of
        # tabs and drops the empty fields between them. This record's
        # fourth field is the launch key, and it is EMPTY for every launch
        # nobody passed --launch-key to. The six generation fields then
        # shift one place left: the boot id lands in the key, the daemon
        # start lands nowhere, and the cross-check below refuses a record
        # that was armed perfectly. Every `sp new --model` without a key
        # ended as a shell with no provider because of this line.
        # The records read above avoid it by switching to a
        # non-whitespace delimiter first; this one does the same.
        IFS= read -r __sk_launch_line < "$__sk_launch"
        __sk_launch_line=${__sk_launch_line//$'\t'/$'\034'}
        IFS=$'\034' read -r __sk_launch_provider __sk_launch_cwd \
          __sk_requested_model __sk_launch_key __sk_launch_boot \
          __sk_launch_started __sk_launch_shell_pid __sk_launch_shell_start \
          __sk_launch_daemon_pid __sk_launch_daemon_start <<<"$__sk_launch_line"
        __sk_validated_model=$(python3 "$__sk_inventory_core" validate-worker-model \
          "$__sk_launch_provider" "$__sk_requested_model" 2>/dev/null || true)
        if [[ $__sk_validated_model == "$__sk_requested_model" &&
              ( -z $__sk_launch_key || $__sk_launch_key =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$ ) ]]; then
          __sk_launch_record_ok=1
        fi
        unset __sk_validated_model
      fi
    fi
    if [[ -n ${SESSION_KIT_BOOT_ID_FILE:-} ]]; then
      __sk_current_boot_id=$(command cat -- "$SESSION_KIT_BOOT_ID_FILE" 2>/dev/null || true)
    elif [[ $(uname -s 2>/dev/null) == Darwin && -f $__sk_inventory_core ]]; then
      __sk_current_boot_id=$(python3 "$__sk_inventory_core" platform boot-id 2>/dev/null || true)
    else
      __sk_current_boot_id=$(command cat -- /proc/sys/kernel/random/boot_id 2>/dev/null || true)
    fi
    __sk_current_boot_id=${__sk_current_boot_id//$'\r'/}
    __sk_current_boot_id=${__sk_current_boot_id//$'\n'/}
    __sk_generation_ok=0
    __sk_proc_identity() {
      local __sk_pid=$1 __sk_stat __sk_tail
      local -a __sk_fields
      if [[ $(uname -s 2>/dev/null) == Darwin ]]; then
        [[ -f $__sk_inventory_core ]] || return 1
        python3 "$__sk_inventory_core" platform process-info "$__sk_pid" 2>/dev/null |
          command tr '\t' ' '
        return
      fi
      [[ -r /proc/$__sk_pid/stat ]] || return 1
      __sk_stat=$(<"/proc/$__sk_pid/stat") || return 1
      __sk_tail=${__sk_stat##*) }
      read -r -a __sk_fields <<<"$__sk_tail"
      [[ ${#__sk_fields[@]} -ge 20 ]] || return 1
      printf '%s %s\n' "${__sk_fields[1]}" "${__sk_fields[19]}"
    }
    __sk_launch_mode_ok=0
    if [[ $__sk_launch_mode == new && -z $__sk_uuid ]]; then
      __sk_launch_mode_ok=1
    elif [[ ( $__sk_launch_mode == resume || $__sk_launch_mode == fork ) &&
            $__sk_provider =~ ^(claude|codex)$ &&
            $__sk_uuid =~ ^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$ ]]; then
      __sk_launch_mode_ok=1
    fi
    if (( __sk_record_shape_ok && __sk_launch_mode_ok && __sk_launch_record_ok )) &&
       [[ $__sk_provider == "$__sk_side_provider" &&
          $__sk_cwd == "$__sk_side_cwd" &&
          $__sk_uuid == "$__sk_side_uuid" &&
          $__sk_launch_mode == "$__sk_side_launch_mode" &&
          -n $__sk_boot_id && $__sk_boot_id == "$__sk_current_boot_id" &&
          $__sk_started =~ ^[0-9]+$ &&
          $__sk_shell_pid =~ ^[0-9]+$ && $__sk_shell_start =~ ^[0-9]+$ &&
          $__sk_daemon_pid =~ ^[0-9]+$ && $__sk_daemon_start =~ ^[0-9]+$ ]]; then
      if [[ -n $__sk_requested_model ]] &&
         [[ $__sk_launch_provider != "$__sk_provider" ||
            $__sk_launch_cwd != "$__sk_cwd" ||
            $__sk_launch_boot != "$__sk_boot_id" ||
            $__sk_launch_started != "$__sk_started" ||
            $__sk_launch_shell_pid != "$__sk_shell_pid" ||
            $__sk_launch_shell_start != "$__sk_shell_start" ||
            $__sk_launch_daemon_pid != "$__sk_daemon_pid" ||
            $__sk_launch_daemon_start != "$__sk_daemon_start" ]]; then
        __sk_launch_record_ok=0
      fi
      __sk_walk_pid=$$
      for __sk_walk_depth in {1..6}; do
        read -r __sk_walk_ppid __sk_walk_start < <(__sk_proc_identity "$__sk_walk_pid") || break
        if [[ $__sk_walk_pid == "$__sk_shell_pid" && $__sk_walk_start == "$__sk_shell_start" ]]; then
          read -r __sk_parent_ppid __sk_parent_start < <(__sk_proc_identity "$__sk_walk_ppid") || break
          if [[ $__sk_walk_ppid == "$__sk_daemon_pid" && $__sk_parent_start == "$__sk_daemon_start" ]]; then
            __sk_generation_ok=1
          fi
          break
        fi
        __sk_walk_pid=$__sk_walk_ppid
      done
    fi
    (( __sk_launch_record_ok )) || __sk_generation_ok=0
    if (( __sk_generation_ok )); then
      unset SESSION_KIT_ACCOUNT_ALIAS
      export SESSION_KIT_ACCOUNT_CAPABLE=0
      if [[ -e $__sk_account || -L $__sk_account ]]; then
        __sk_account_ok=0
        __sk_account_provider= __sk_account_alias= __sk_account_profile=
        if [[ -f $__sk_account && ! -L $__sk_account ]] &&
           python3 - "$__sk_account" <<'PY'
import os
import stat
import sys

path = sys.argv[1]
info = os.lstat(path)
with open(path, "rb") as stream:
    payload = stream.read(257)
if (
    not stat.S_ISREG(info.st_mode)
    or info.st_uid != os.geteuid()
    or stat.S_IMODE(info.st_mode) != 0o600
    or not payload.endswith(b"\n")
    or payload.count(b"\n") != 1
    or payload[:-1].count(b"\t") != 1
    or b"\r" in payload
    or b"\x1c" in payload
):
    raise SystemExit(1)
PY
        then
          IFS=$'\t' read -r __sk_account_provider __sk_account_alias < "$__sk_account"
          if [[ $__sk_account_provider == "$__sk_provider" &&
                $__sk_account_alias =~ ^[a-z][a-z0-9_-]{0,11}$ ]]; then
            __sk_account_json=$(python3 "$__sk_inventory_core" account resume-profile \
              "$__sk_account_provider" "$__sk_account_alias" 2>/dev/null || true)
            __sk_account_profile=$(python3 -c '
import json,sys
try:
    value=json.loads(sys.stdin.read())
except ValueError:
    raise SystemExit(1)
profile=value.get("profile_dir")
if value.get("provider") != sys.argv[1] or value.get("alias") != sys.argv[2]:
    raise SystemExit(1)
if not isinstance(profile,str) or not profile.startswith("/") or "\n" in profile or "\r" in profile:
    raise SystemExit(1)
print(profile)
' "$__sk_provider" "$__sk_account_alias" <<<"$__sk_account_json" 2>/dev/null || true)
            if [[ $__sk_account_profile == /* ]]; then
              export SESSION_KIT_ACCOUNT_ALIAS=$__sk_account_alias
              export SESSION_KIT_ACCOUNT_CAPABLE=1
              if [[ $__sk_provider == claude ]]; then
                export CLAUDE_CONFIG_DIR=$__sk_account_profile
              else
                export CODEX_HOME=$__sk_account_profile
                export SESSION_KIT_CODEX_HOME=$__sk_account_profile
              fi
              __sk_account_ok=1
            fi
          fi
        fi
        if (( ! __sk_account_ok )); then
          echo "[session-kit: selected account profile is unsafe or no longer matches its login; provider not started]" >&2
          __sk_log_launch refused_account_unsafe
          __sk_generation_ok=0
        fi
        unset __sk_account_json
      fi
    fi
    if (( ! __sk_generation_ok )); then
      echo "[session-kit: stale or mismatched launch record retained; provider not started]" >&2
      __sk_log_launch refused_generation_mismatch
    elif [[ $__sk_cwd == /* && -d $__sk_cwd ]]; then
      if ! cd -- "$__sk_cwd"; then
        echo "[session-kit: launch directory is unavailable; launch record retained for retry]" >&2
        __sk_log_launch refused_directory_unavailable
      else
        __sk_provider_launched=0
        __sk_provider_exited=0
        __sk_provider_rc=0
        __sk_lifecycle_uuid=
        __sk_lifecycle_intake=
        __sk_lifecycle_generation=
        __sk_model_args=()
        __sk_launch_model=$__sk_requested_model
        __sk_model_carried=0
        # An account switch relaunches this conversation by re-entering here
        # through `exec bash -i`, and the launch record that carried the model
        # was consumed at first launch. Without this the provider is started
        # with no --model at all and chooses its own: the model changing by
        # itself, which is the one thing the model rule says never happens
        # quietly. SESSION_KIT_CARRIED_MODEL is set by that switch and nowhere
        # else -- it comes from the same process that launched the provider,
        # not from a file anything else can write -- and it is still put
        # through the provider's validator before it is used.
        if [[ -n ${SESSION_KIT_CARRIED_MODEL:-} ]]; then
          if [[ -z $__sk_launch_model ]]; then
            __sk_carried_model=$(python3 "$__sk_inventory_core" validate-worker-model \
              "$__sk_provider" "$SESSION_KIT_CARRIED_MODEL" 2>/dev/null || true)
            if [[ -n $__sk_carried_model &&
                  $__sk_carried_model == "$SESSION_KIT_CARRIED_MODEL" ]]; then
              __sk_launch_model=$__sk_carried_model
              __sk_model_carried=1
            else
              # Said before the session starts, never discovered afterwards.
              echo "[session-kit: this session was started on ${SESSION_KIT_CARRIED_MODEL}, and that could not be carried across the restart; it comes back on ${__sk_provider}'s own default. Use sp change-model to put it back.]" >&2
            fi
            unset __sk_carried_model
          fi
          unset SESSION_KIT_CARRIED_MODEL
        fi
        if [[ -n $__sk_launch_model ]]; then
          __sk_model_args=(--model "$__sk_launch_model")
          export SESSION_KIT_REQUESTED_MODEL=$__sk_launch_model
          if (( __sk_model_carried )); then
            # A carried relaunch is a new launch and has no key of its own; the
            # old one belongs to a launch that already happened.
            unset SESSION_KIT_LAUNCH_IDEMPOTENCY_KEY
          else
            export SESSION_KIT_LAUNCH_IDEMPOTENCY_KEY=$__sk_launch_key
          fi
        else
          unset SESSION_KIT_REQUESTED_MODEL SESSION_KIT_LAUNCH_IDEMPOTENCY_KEY
        fi
        unset __sk_model_carried
        case "$__sk_provider" in
        claude)
          if ! command -v claude >/dev/null 2>&1; then
            echo "[session-kit: Claude is unavailable; launch record retained for retry]" >&2
            __sk_log_launch refused_provider_missing
          elif [[ $__sk_launch_mode != new ]] || \
              __sk_new_claude_uuid=$(python3 -c 'import uuid; print(uuid.uuid4())'); then
            __sk_consumed_records=("$__sk_start" "$__sk_expected")
            [[ -z $__sk_requested_model ]] || __sk_consumed_records+=("$__sk_launch")
            [[ ! -e $__sk_account ]] || __sk_consumed_records+=("$__sk_account")
            command rm -- "${__sk_consumed_records[@]}"
            unset __sk_consumed_records
            __sk_provider_launched=1
            __sk_log_launch started
            if [[ $__sk_launch_mode == new ]]; then
              __sk_lifecycle_uuid=$__sk_new_claude_uuid
            else
              __sk_lifecycle_uuid=$__sk_uuid
            fi
            # A Claude window renames only through its SessionStart/prompt
            # hooks, so a session that boots before its name intent exists
            # (fresh uuid, or a pre-baked resume) shows a stale title until
            # the provider restarts. The marker lets the picker request one
            # safe bounce once a name exists — same contract as Codex.
            __sk_claude_intent_uuid=$__sk_uuid
            [[ $__sk_launch_mode == new ]] && __sk_claude_intent_uuid=$__sk_new_claude_uuid
            if [[ -n $__sk_claude_intent_uuid && \
                  ! -f "$HOME/.claude/sessions/$__sk_claude_intent_uuid.nameintent" ]]; then
              ( umask 077
                mkdir -p "$__sk_state_root/session-kit/provider-untitled" &&
                  printf '%s\n' \
                    "$__sk_current_boot_id:$__sk_shell_pid:$__sk_shell_start:$RANDOM:$EPOCHREALTIME" \
                    > "$__sk_state_root/session-kit/provider-untitled/$SHPOOL_SESSION_NAME"
              ) 2>/dev/null || true
            fi
            unset __sk_claude_intent_uuid
            __sk_mcp_args=()
            __sk_resume_degraded=0
            while :; do
              # A resume of a conversation this profile does not hold can only
              # fail. The pre-bake mints its throwaway inside one profile and
              # this shell resolves its own; if they ever disagree, the launch
              # records are already consumed, so the old code ran `--resume`,
              # got "No conversation found", took that for a crash, reopened
              # the SAME conversation once, crashed again, and left an open
              # window with no provider in it at all. A conversation nothing
              # can find is nothing to lose: start a fresh one instead, which
              # still takes its colour from the kit at attach.
              if [[ $__sk_launch_mode == resume && $__sk_resume_degraded == 0 ]] &&
                 ! compgen -G "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/*/$__sk_uuid.jsonl" >/dev/null 2>&1; then
                __sk_resume_degraded=1
                if __sk_new_claude_uuid=$(python3 -c 'import uuid; print(uuid.uuid4())'); then
                  __sk_launch_mode=new
                  __sk_lifecycle_uuid=$__sk_new_claude_uuid
                  __sk_log_launch degraded_resume_to_new
                  echo "[session-kit: the pre-started conversation was not in this profile; opening a new one instead]" >&2
                else
                  echo "[session-kit: Claude session identity could not be allocated; resuming as asked]" >&2
                fi
              fi
              case "$__sk_launch_mode" in
                new) claude "${__sk_model_args[@]}" --session-id "$__sk_new_claude_uuid" "${__sk_mcp_args[@]}"; __sk_provider_rc=$? ;;
                resume) claude "${__sk_model_args[@]}" --resume "$__sk_uuid" "${__sk_mcp_args[@]}"; __sk_provider_rc=$? ;;
                fork) claude "${__sk_model_args[@]}" --resume "$__sk_uuid" --fork-session "${__sk_mcp_args[@]}"; __sk_provider_rc=$? ;;
              esac
              # Same rule after the fact: a resume that exited nonzero without
              # its conversation being anywhere this profile can see is not a
              # crash to reopen, it is a launch to redo as a new session. Once
              # only -- never a loop.
              if [[ $__sk_launch_mode == resume && $__sk_provider_rc -ne 0 && $__sk_resume_degraded == 0 ]] &&
                 ! compgen -G "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/*/$__sk_uuid.jsonl" >/dev/null 2>&1 &&
                 __sk_new_claude_uuid=$(python3 -c 'import uuid; print(uuid.uuid4())'); then
                __sk_resume_degraded=1
                __sk_launch_mode=new
                __sk_lifecycle_uuid=$__sk_new_claude_uuid
                __sk_log_launch degraded_resume_to_new
                echo "[session-kit: that conversation could not be resumed; opening a new one instead]" >&2
                continue
              fi
              # A kit-requested bounce relaunches the SAME conversation once,
              # so the fresh process boots through SessionStart with its name
              # intent applied. Any other exit falls through to the normal
              # provider-exit handling.
              __sk_bounce="$__sk_state_root/session-kit/provider-bounce/$SHPOOL_SESSION_NAME"
              if [[ -f $__sk_bounce && ! -L $__sk_bounce ]]; then
                __sk_bounce_uuid=$(command head -c 64 -- "$__sk_bounce" 2>/dev/null | tr -cd '0-9a-fA-F-')
                if [[ $__sk_bounce_uuid =~ ^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$ ]]; then
                  __sk_uuid=$__sk_bounce_uuid
                  __sk_launch_mode=resume
                  # RENAMED, not removed. The marker is the only thing keeping
                  # this session in the person's list while the kit has its
                  # window shut, and there is no moment in the relaunch at
                  # which this shell can prove the replacement is up -- it
                  # blocks on the provider. So the marker stays as a receipt
                  # whose generated suffix identifies this exact request.
                  # Request and receipt names both read as bouncing, so the
                  # row stays visible either way; the rename is what
                  # stops this same block from bouncing forever, because the
                  # next read finds no marker under the first name.
                  #
                  # A generated receipt NAME rather than an emptied file or a
                  # reusable path binds settlement to one request generation.
                  # Collection captures the eligible names before it reads the
                  # process table, so an older sighting cannot name or delete a
                  # receipt installed after that read began.
                  #
                  # What ends the bounce is a positive sighting of the
                  # replacement window, which only collection can make. If the
                  # replacement never comes up, nothing clears it and the row
                  # stays visible, which is the correct way to be wrong.
                  __sk_bounce_generation=$(command sed -n '3p' -- "$__sk_bounce" 2>/dev/null | tr -cd 'A-Za-z0-9')
                  if [[ $__sk_bounce_generation =~ ^[A-Za-z0-9]{6,32}$ ]]; then
                    __sk_bounce_receipt="$__sk_bounce.taken.$__sk_bounce_generation"
                  else
                    # A request written before generated receipts shipped is
                    # still honoured. Its reusable legacy receipt is never
                    # settled by collection, so an already-live old shell can
                    # never have its protection removed by stale evidence.
                    __sk_bounce_receipt="$__sk_bounce.taken"
                  fi
                  command mv -f -- "$__sk_bounce" "$__sk_bounce_receipt" 2>/dev/null ||
                    { : > "$__sk_bounce"; } 2>/dev/null || true
                  unset __sk_bounce_uuid __sk_bounce_generation __sk_bounce_receipt __sk_bounce
                  continue
                fi
                command rm -f -- "$__sk_bounce"
                unset __sk_bounce_uuid
              fi
              # The provider loop is over, so a marker under either name can
              # never be consumed or settled again. Left behind, it would keep
              # this id reading as bouncing for as long as the state directory
              # survives -- the sweep would get it eventually, but only after
              # the grace, and only once the session is gone from the listing.
              command rm -f -- "$__sk_bounce" "$__sk_bounce.taken"
              for __sk_bounce_receipt in "$__sk_bounce".taken.*; do
                [[ -e $__sk_bounce_receipt || -L $__sk_bounce_receipt ]] || continue
                command rm -f -- "$__sk_bounce_receipt"
              done
              unset __sk_bounce_receipt
              unset __sk_bounce
              break
            done
          else
            echo "[session-kit: Claude session identity could not be allocated; launch record retained for retry]" >&2
          fi
          unset __sk_new_claude_uuid __sk_mcp_args __sk_resume_degraded
          ;;
        codex)
          if ! command -v codex >/dev/null 2>&1; then
            echo "[session-kit: Codex is unavailable; launch record retained for retry]" >&2
            __sk_log_launch refused_provider_missing
          else
            # A managed session is launched with nobody watching it. Codex's
            # startup upgrade prompt blocks on a keypress that never comes, so
            # the session sits at "setup incomplete" forever and its
            # conversation never loads. Suppressed explicitly here rather than
            # relying on user config.
            __sk_codex_no_update=(-c check_for_update_on_startup=false)
            # A private per-repository coordination config can opt future Codex
            # sessions into a local App Server plus an exact-thread broker.
            # Existing direct sessions are never changed. Invalid, unsafe, or
            # absent config fails back to the normal direct TUI launch.
            __sk_coord_gate=
            __sk_coord_broker=
            __sk_coord_config="$HOME/.config/session-kit/coordination.json"
            if [[ -f $__sk_coord_config && ! -L $__sk_coord_config ]]; then
              __sk_coord_gate=$(python3 - "$__sk_coord_config" "$__sk_cwd" <<'PY'
import json
import os
import stat
import sys

config_path, launch_cwd = sys.argv[1:]
try:
    metadata = os.lstat(config_path)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
        or metadata.st_size > 8192
    ):
        raise ValueError("unsafe config")
    with open(config_path, encoding="utf-8") as stream:
        config = json.load(stream)
    repo_root = os.path.realpath(config["repo_root"])
    broker = config["codex_broker"]
    if (
        config.get("codex_app_server") is not True
        or not os.path.isabs(broker)
        or not os.path.isfile(broker)
        or os.path.islink(broker)
    ):
        raise ValueError("inactive config")
    broker_metadata = os.stat(broker)
    if broker_metadata.st_uid != os.geteuid() or broker_metadata.st_mode & 0o022:
        raise ValueError("unsafe broker")
    # "codex_app_server_all" arms the App Server for every cwd so any managed
    # Codex window is reachable. The exact-thread broker stays repo-scoped: a
    # session outside the repository gets the server alone. Without the key the
    # gate is repo-only, exactly as before.
    if os.path.realpath(launch_cwd) != repo_root:
        if config.get("codex_app_server_all") is not True:
            raise ValueError("inactive config")
        print("-")
    else:
        print(broker)
except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
    pass
PY
              )
            fi
            # An absolute path arms the server and its broker; "-" arms the
            # server alone. Any other answer leaves the gate shut.
            case $__sk_coord_gate in
              /*) __sk_coord_broker=$__sk_coord_gate ;;
              -) ;;
              *) __sk_coord_gate= ;;
            esac
            __sk_codex_remote=()
            __sk_app_server_pid=
            __sk_broker_pid=
            __sk_app_socket=
            if [[ -n $__sk_coord_gate ]]; then
              __sk_app_dir=$(python3 "$__sk_inventory_core" platform app-server-dir \
                "$__sk_state_root" "$SHPOOL_SESSION_NAME" 2>/dev/null || true)
              if [[ -z $__sk_app_dir ]]; then
                # The private directory chain refused, so the gate quietly used
                # the direct TUI and every reachability feature stayed dark for
                # the life of the window. Name the reason once — on stderr, and
                # in this session's App Server log when one already exists.
                python3 - "$__sk_state_root" "$SHPOOL_SESSION_NAME" <<'PY' || true
import os
import stat
import sys

state_root, session_id = sys.argv[1:]
line = "[session-kit: Codex App Server disabled — ~/.local/state must be mode 0700]"
try:
    metadata = os.lstat(state_root)
    private = (
        stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and stat.S_IMODE(metadata.st_mode) == 0o700
    )
except OSError:
    private = False
if private:
    line = (
        "[session-kit: Codex App Server disabled — "
        "its private state directory is unavailable]"
    )
print(line, file=sys.stderr)
if not session_id or "/" in session_id or session_id.startswith("."):
    raise SystemExit(0)
log = os.path.join(
    state_root, "session-kit", "app-server", session_id, "app-server.log"
)
flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
flags |= getattr(os, "O_NOFOLLOW", 0)
try:
    descriptor = os.open(log, flags)
except OSError:
    raise SystemExit(0)
try:
    log_metadata = os.fstat(descriptor)
    if stat.S_ISREG(log_metadata.st_mode) and log_metadata.st_uid == os.geteuid():
        os.write(descriptor, (line + "\n").encode())
finally:
    os.close(descriptor)
PY
              fi
              if [[ -n $__sk_app_dir ]]; then
                __sk_app_socket="$__sk_app_dir/app.sock"
                __sk_app_log="$__sk_app_dir/app-server.log"
                __sk_broker_log="$__sk_app_dir/broker.log"
                if [[ -L $__sk_app_log || -L $__sk_broker_log ]]; then
                  echo "[session-kit: refusing symlinked App Server log]" >&2
                elif ( umask 077
                  python3 - "$__sk_app_log" "$__sk_broker_log" <<'PY'
import os, stat, sys

for path in sys.argv[1:]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            raise OSError("unsafe App Server log")
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
PY
                ) && [[ ! -L $__sk_app_log && ! -L $__sk_broker_log ]] &&
                   [[ -f $__sk_app_log && -f $__sk_broker_log ]] &&
                   chmod 600 -- "$__sk_app_log" "$__sk_broker_log" &&
                   [[ ! -e $__sk_app_socket ]]; then
                  codex "${__sk_codex_no_update[@]}" app-server \
                    --listen "unix://$__sk_app_socket" \
                    >>"$__sk_app_log" 2>&1 &
                  __sk_app_server_pid=$!
                  for __sk_app_attempt in {1..50}; do
                    [[ -S $__sk_app_socket ]] && break
                    kill -0 "$__sk_app_server_pid" 2>/dev/null || break
                    sleep 0.1
                  done
                  if [[ -S $__sk_app_socket ]] && \
                     kill -0 "$__sk_app_server_pid" 2>/dev/null; then
                    chmod 600 -- "$__sk_app_socket" 2>/dev/null || true
                    __sk_codex_remote=(--remote "unix://$__sk_app_socket")
                    if [[ -n $__sk_coord_broker ]]; then
                      __sk_broker_args=(
                        --socket "$__sk_app_socket"
                        --repo "$__sk_cwd"
                        --server-pid "$__sk_app_server_pid"
                      )
                      [[ $__sk_launch_mode == resume && -n $__sk_uuid ]] && \
                        __sk_broker_args+=(--thread "$__sk_uuid")
                      python3 "$__sk_coord_broker" "${__sk_broker_args[@]}" \
                        >>"$__sk_broker_log" 2>&1 &
                      __sk_broker_pid=$!
                    fi
                  else
                    if [[ -n $__sk_app_server_pid ]]; then
                      kill "$__sk_app_server_pid" 2>/dev/null || true
                      wait "$__sk_app_server_pid" 2>/dev/null || true
                    fi
                    __sk_app_server_pid=
                    echo "[session-kit: Codex App Server unavailable; using direct TUI]" >&2
                  fi
                fi
              fi
            fi
            # Codex has no per-thread color; the session's kit color rides in
            # as a per-launch theme override (status line, thread-title item).
            # Resumes and forks color from the conversation's effective color;
            # a brand-new session has no conversation ID yet, so it launches
            # with a color picked from the shpool session name — the collector
            # adopts that pick as the conversation's override once the ID
            # exists, keeping window, picker, and future resumes identical.
            # Fail-open: unknown color or missing theme file launches plain.
            __sk_consumed_records=("$__sk_start" "$__sk_expected")
            [[ -z $__sk_requested_model ]] || __sk_consumed_records+=("$__sk_launch")
            [[ ! -e $__sk_account ]] || __sk_consumed_records+=("$__sk_account")
            command rm -- "${__sk_consumed_records[@]}"
            unset __sk_consumed_records
            __sk_provider_launched=1
            # A Codex process that boots before a real thread title exists
            # keeps showing the conversation ID for that process's life. The
            # marker lets the picker request one safe provider bounce once a
            # title exists, including a resumed thread that was still untitled.
            if [[ $__sk_launch_mode == new ]] ||
               ! python3 "$__sk_inventory_core" codex-bounce-title --read-only "$__sk_uuid" >/dev/null 2>&1; then
              ( umask 077
                mkdir -p "$__sk_state_root/session-kit/provider-untitled" &&
                  printf '%s\n' \
                    "$__sk_current_boot_id:$__sk_shell_pid:$__sk_shell_start:$RANDOM:$EPOCHREALTIME" \
                    > "$__sk_state_root/session-kit/provider-untitled/$SHPOOL_SESSION_NAME"
              ) 2>/dev/null || true
            fi
            while :; do
              __sk_codex_theme=()
              # The kit owns the tab name on both providers (K3). Codex writes
              # its own OSC title from tui.terminal_title, so the kit hands it
              # the item list it deployed -- the state glyph plus the thread
              # name, which is the string the kit writes when a session is
              # named -- as a per-launch override. The personal
              # ~/.codex/config.toml is never touched: an override on the
              # command line wins for this process only (verified against
              # codex-cli 0.145.0, 2026-08-13). Kill switch, both providers:
              # SESSION_KIT_TAB_TITLE=off.
              __sk_codex_title=()
              __sk_codex_title_switch=${SESSION_KIT_TAB_TITLE:-}
              # Same rule as sk_tab_title and doctor: surrounding whitespace
              # is immaterial, and every spelling of "off" disables titles.
              __sk_codex_title_switch=${__sk_codex_title_switch#"${__sk_codex_title_switch%%[![:space:]]*}"}
              __sk_codex_title_switch=${__sk_codex_title_switch%"${__sk_codex_title_switch##*[![:space:]]}"}
              if [[ ${__sk_codex_title_switch,,} != off ]]; then
                __sk_codex_title_items='["activity", "thread"]'
                __sk_codex_title_file=${SESSION_KIT_CODEX_HOME:-${CODEX_HOME:-$HOME/.codex}}/session-kit/terminal-title.toml
                if [[ -f $__sk_codex_title_file && ! -L $__sk_codex_title_file ]]; then
                  # Parsed as real TOML, not matched as text. A value like
                  # ["thread] passes any reasonable regex and is unterminated
                  # TOML -- passing it through on `-c` would stop Codex from
                  # starting at all, turning an owner's typo in a file the kit
                  # deployed into a session that never opens. Anything this
                  # cannot parse into a list of known-shaped item names falls
                  # back to the built-in value, and the same file is checked by
                  # `session-kit doctor`.
                  __sk_codex_title_deployed=$(
                    python3 - "$__sk_codex_title_file" <<'SKTITLE' 2>/dev/null
import re
import sys

try:
    import tomllib
except ImportError:  # Python older than 3.11 has no parser; use the default.
    raise SystemExit(1)

try:
    with open(sys.argv[1], "rb") as handle:
        value = tomllib.load(handle).get("tui", {}).get("terminal_title")
except (OSError, ValueError, tomllib.TOMLDecodeError):
    raise SystemExit(1)
if not isinstance(value, list) or not value or len(value) > 12:
    raise SystemExit(1)
items = []
for item in value:
    if not isinstance(item, str) or not re.fullmatch(r"[a-z][a-z-]{0,31}", item):
        raise SystemExit(1)
    items.append(item)
print("[" + ", ".join('"%s"' % item for item in items) + "]")
SKTITLE
                  ) || __sk_codex_title_deployed=
                  if [[ -n $__sk_codex_title_deployed ]]; then
                    __sk_codex_title_items=$__sk_codex_title_deployed
                  fi
                  unset __sk_codex_title_deployed
                fi
                __sk_codex_title=(-c "tui.terminal_title=$__sk_codex_title_items")
                unset __sk_codex_title_items __sk_codex_title_file
              fi
              unset __sk_codex_title_switch
              __sk_theme_color=
              if [[ -f $__sk_inventory_core ]]; then
                if [[ -n $__sk_uuid ]]; then
                  __sk_theme_color=$(python3 "$__sk_inventory_core" color effective codex "$__sk_uuid" 2>/dev/null || true)
                else
                  __sk_theme_color=$(python3 "$__sk_inventory_core" color launch-pick "$SHPOOL_SESSION_NAME" 2>/dev/null || true)
                fi
              fi
              case "$__sk_theme_color" in
                lime|magenta|silver|sand|sky|sea)
                  __sk_codex_home=${SESSION_KIT_CODEX_HOME:-${CODEX_HOME:-$HOME/.codex}}
                  if [[ -r $__sk_codex_home/themes/sk-$__sk_theme_color.tmTheme ]]; then
                    __sk_codex_theme=(-c "tui.theme=\"sk-$__sk_theme_color\"")
                  fi
                  unset __sk_codex_home
                  ;;
              esac
              unset __sk_theme_color
              case "$__sk_launch_mode" in
                new)
                  # A prompt staged by `sp new --prompt-file` seeds the session
                  # directly (the intake ceremony is retired; the seeded launch
                  # is not). Consume-once: the handoff never survives a launch.
                  __sk_prompt_handoff="$__sk_start_dir/$SHPOOL_SESSION_NAME.prompt"
                  __sk_seed_prompt=
                  if [[ -f $__sk_prompt_handoff && ! -L $__sk_prompt_handoff ]]; then
                    __sk_seed_prompt=$(command cat -- "$__sk_prompt_handoff" 2>/dev/null) ||
                      __sk_seed_prompt=
                    command rm -f -- "$__sk_prompt_handoff"
                  fi
                  if [[ -n $__sk_seed_prompt ]]; then
                    codex "${__sk_codex_no_update[@]}" "${__sk_codex_theme[@]}" \
                      "${__sk_codex_title[@]}" \
                      "${__sk_model_args[@]}" "${__sk_codex_remote[@]}" --no-alt-screen \
                      -- "$__sk_seed_prompt"
                  else
                    codex "${__sk_codex_no_update[@]}" "${__sk_codex_theme[@]}" \
                      "${__sk_codex_title[@]}" \
                      "${__sk_model_args[@]}" "${__sk_codex_remote[@]}" --no-alt-screen
                  fi
                  __sk_provider_rc=$?
                  unset __sk_prompt_handoff __sk_seed_prompt
                  ;;
                resume) codex "${__sk_codex_no_update[@]}" "${__sk_codex_theme[@]}" "${__sk_codex_title[@]}" "${__sk_model_args[@]}" "${__sk_codex_remote[@]}" --no-alt-screen resume "$__sk_uuid"; __sk_provider_rc=$? ;;
                fork) codex "${__sk_codex_no_update[@]}" "${__sk_codex_theme[@]}" "${__sk_codex_title[@]}" "${__sk_model_args[@]}" "${__sk_codex_remote[@]}" --no-alt-screen fork "$__sk_uuid"; __sk_provider_rc=$? ;;
              esac
              if [[ $__sk_launch_mode != new ]]; then
                __sk_lifecycle_uuid=$__sk_uuid
              fi
              # A kit-requested bounce relaunches the SAME conversation once,
              # so the fresh process boots with its title and theme. Any other
              # exit falls through to the normal provider-exit handling.
              __sk_bounce="$__sk_state_root/session-kit/provider-bounce/$SHPOOL_SESSION_NAME"
              if [[ -f $__sk_bounce && ! -L $__sk_bounce ]]; then
                __sk_bounce_uuid=$(command head -c 64 -- "$__sk_bounce" 2>/dev/null | tr -cd '0-9a-fA-F-')
                if [[ $__sk_bounce_uuid =~ ^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$ ]]; then
                  __sk_uuid=$__sk_bounce_uuid
                  __sk_launch_mode=resume
                  # RENAMED, not removed. The marker is the only thing keeping
                  # this session in the person's list while the kit has its
                  # window shut, and there is no moment in the relaunch at
                  # which this shell can prove the replacement is up -- it
                  # blocks on the provider. So the marker stays as a receipt
                  # whose generated suffix identifies this exact request.
                  # Request and receipt names both read as bouncing, so the
                  # row stays visible either way; the rename is what
                  # stops this same block from bouncing forever, because the
                  # next read finds no marker under the first name.
                  #
                  # A generated receipt NAME rather than an emptied file or a
                  # reusable path binds settlement to one request generation.
                  # Collection captures the eligible names before it reads the
                  # process table, so an older sighting cannot name or delete a
                  # receipt installed after that read began.
                  #
                  # What ends the bounce is a positive sighting of the
                  # replacement window, which only collection can make. If the
                  # replacement never comes up, nothing clears it and the row
                  # stays visible, which is the correct way to be wrong.
                  __sk_bounce_generation=$(command sed -n '3p' -- "$__sk_bounce" 2>/dev/null | tr -cd 'A-Za-z0-9')
                  if [[ $__sk_bounce_generation =~ ^[A-Za-z0-9]{6,32}$ ]]; then
                    __sk_bounce_receipt="$__sk_bounce.taken.$__sk_bounce_generation"
                  else
                    # A request written before generated receipts shipped is
                    # still honoured. Its reusable legacy receipt is never
                    # settled by collection, so an already-live old shell can
                    # never have its protection removed by stale evidence.
                    __sk_bounce_receipt="$__sk_bounce.taken"
                  fi
                  command mv -f -- "$__sk_bounce" "$__sk_bounce_receipt" 2>/dev/null ||
                    { : > "$__sk_bounce"; } 2>/dev/null || true
                  unset __sk_bounce_uuid __sk_bounce_generation __sk_bounce_receipt __sk_bounce
                  continue
                fi
                command rm -f -- "$__sk_bounce"
                unset __sk_bounce_uuid
              fi
              # The provider loop is over, so a marker under either name can
              # never be consumed or settled again. Left behind, it would keep
              # this id reading as bouncing for as long as the state directory
              # survives -- the sweep would get it eventually, but only after
              # the grace, and only once the session is gone from the listing.
              command rm -f -- "$__sk_bounce" "$__sk_bounce.taken"
              for __sk_bounce_receipt in "$__sk_bounce".taken.*; do
                [[ -e $__sk_bounce_receipt || -L $__sk_bounce_receipt ]] || continue
                command rm -f -- "$__sk_bounce_receipt"
              done
              unset __sk_bounce_receipt
              unset __sk_bounce
              break
            done
            if [[ -n $__sk_broker_pid ]]; then
              kill "$__sk_broker_pid" 2>/dev/null || true
              wait "$__sk_broker_pid" 2>/dev/null || true
            fi
            if [[ -n $__sk_app_server_pid ]]; then
              kill "$__sk_app_server_pid" 2>/dev/null || true
              wait "$__sk_app_server_pid" 2>/dev/null || true
            fi
            [[ -n $__sk_app_socket && -S $__sk_app_socket ]] && \
              command rm -- "$__sk_app_socket"
          fi
          ;;
        shell)
          command rm -- "$__sk_start" "$__sk_expected"
          ;;
        esac
        if (( __sk_provider_launched )); then
          __sk_lifecycle_core=$__sk_inventory_core
          __sk_lifecycle_session=$SHPOOL_SESSION_NAME
          __sk_lifecycle_boot=$__sk_current_boot_id
          __sk_lifecycle_shell_pid=$__sk_shell_pid
          __sk_lifecycle_shell_start=$__sk_shell_start
          __sk_lifecycle_provider=$__sk_provider
          if SESSION_KIT_LIFECYCLE_SESSION_ID=$__sk_lifecycle_session \
             SESSION_KIT_LIFECYCLE_BOOT_ID=$__sk_lifecycle_boot \
             SESSION_KIT_LIFECYCLE_SHELL_PID=$__sk_lifecycle_shell_pid \
             SESSION_KIT_LIFECYCLE_SHELL_START_TICKS=$__sk_lifecycle_shell_start \
             SESSION_KIT_LIFECYCLE_PROVIDER=$__sk_lifecycle_provider \
             SESSION_KIT_LIFECYCLE_EXIT_CODE=$__sk_provider_rc \
             SESSION_KIT_LIFECYCLE_CONVERSATION_UUID=$__sk_lifecycle_uuid \
             SESSION_KIT_LIFECYCLE_INTAKE_COMMIT=$__sk_lifecycle_intake \
             SESSION_KIT_MANAGED_GENERATION=$__sk_lifecycle_generation \
             SESSION_KIT_REQUESTED_MODEL=$__sk_requested_model \
             SESSION_KIT_LAUNCH_IDEMPOTENCY_KEY=$__sk_launch_key \
             python3 "$__sk_lifecycle_core" lifecycle provider-exited >/dev/null
          then
            __sk_provider_exited=1
            __sk_provider_exit_code=$__sk_provider_rc
          elif sleep 0.5 && \
             SESSION_KIT_LIFECYCLE_SESSION_ID=$__sk_lifecycle_session \
             SESSION_KIT_LIFECYCLE_BOOT_ID=$__sk_lifecycle_boot \
             SESSION_KIT_LIFECYCLE_SHELL_PID=$__sk_lifecycle_shell_pid \
             SESSION_KIT_LIFECYCLE_SHELL_START_TICKS=$__sk_lifecycle_shell_start \
             SESSION_KIT_LIFECYCLE_PROVIDER=$__sk_lifecycle_provider \
             SESSION_KIT_LIFECYCLE_EXIT_CODE=$__sk_provider_rc \
             SESSION_KIT_LIFECYCLE_CONVERSATION_UUID=$__sk_lifecycle_uuid \
             SESSION_KIT_LIFECYCLE_INTAKE_COMMIT=$__sk_lifecycle_intake \
             SESSION_KIT_MANAGED_GENERATION=$__sk_lifecycle_generation \
             SESSION_KIT_REQUESTED_MODEL=$__sk_requested_model \
             SESSION_KIT_LAUNCH_IDEMPOTENCY_KEY=$__sk_launch_key \
             python3 "$__sk_lifecycle_core" lifecycle provider-exited >/dev/null
          then
            # One retry, half a second apart. A momentary failure -- a killed
            # interpreter, a full disk clearing -- used to be permanent: the
            # record is what makes the row say "provider exited", and the
            # cleanup pass only ever considers a row that says it
            # (lib/sessionkit_inventory/reaper.py). Without it the session
            # looked like an ordinary idle shell forever, so nothing ever
            # closed it and the person was left with a row to clear by hand.
            __sk_provider_exited=1
            __sk_provider_exit_code=$__sk_provider_rc
          else
            # Still no proof. The session stays open on purpose: with no
            # record there is nothing that could restore this conversation, so
            # ending it would lose it. Both the sentence and the hand-back
            # happen LATER, at the marker below.
            #
            # They used to happen here, and the hand-back could not: this is
            # line ~885 of a file that defines __sk_leave_after_provider_exit
            # at ~1190, and a bashrc runs top to bottom. So the one path meant
            # to stop the shell "dropping silently into a shell nobody asked
            # for" printed `__sk_leave_after_provider_exit: command not found`
            # and then dropped silently into a shell nobody asked for -- with
            # an internal function name on the operator's screen. The test
            # named ..._says_so_and_hands_back asserted the two sentences and
            # never the hand-back its own name promised (found in review,
            # 2026-08-15).
            __sk_record_unwritable=1
          fi
          # An account switch is requested from a different Session Kit
          # window.  The exact provider has exited and any Codex App Server
          # children have already been reaped, so it is now safe to move the
          # exact transcript and re-enter this same managed shell generation.
          __sk_switch_request="$__sk_kit_state/account-switch-requests/$SHPOOL_SESSION_NAME"
          if [[ -e $__sk_switch_request || -L $__sk_switch_request ]]; then
            __sk_switch_ok=0
            __sk_switch_action= __sk_switch_txid= __sk_switch_alias=
            __sk_switch_fallback= __sk_switch_uuid=
            __sk_switch_started= __sk_switch_shell_pid= __sk_switch_shell_start=
            __sk_switch_model= __sk_switch_model_ok=0
            if [[ -f $__sk_switch_request && ! -L $__sk_switch_request ]] &&
               python3 - "$__sk_switch_request" <<'PY'
import os
import stat
import sys

path=sys.argv[1]
info=os.lstat(path)
with open(path,"rb") as stream:
    payload=stream.read(513)
if (
    not stat.S_ISREG(info.st_mode)
    or info.st_uid != os.geteuid()
    or stat.S_IMODE(info.st_mode) != 0o600
    or not payload.endswith(b"\n")
    or payload.count(b"\n") != 1
    or payload[:-1].count(b"\t") != 8
    or b"\r" in payload
    or b"\x1c" in payload
):
    raise SystemExit(1)
PY
            then
              IFS= read -r __sk_switch_line < "$__sk_switch_request"
              __sk_switch_line=${__sk_switch_line//$'\t'/$'\034'}
              IFS=$'\034' read -r __sk_switch_action __sk_switch_txid \
                __sk_switch_alias __sk_switch_fallback __sk_switch_uuid \
                __sk_switch_started __sk_switch_shell_pid __sk_switch_shell_start \
                __sk_switch_model <<<"$__sk_switch_line"
              if [[ -z $__sk_switch_model && -z $__sk_requested_model ]]; then
                __sk_switch_model_ok=1
              elif [[ $__sk_switch_model == "$__sk_requested_model" ]]; then
                __sk_switch_validated_model=$(python3 "$__sk_inventory_core" \
                  validate-worker-model "$__sk_provider" "$__sk_switch_model" \
                  2>/dev/null || true)
                [[ $__sk_switch_validated_model == "$__sk_switch_model" ]] &&
                  __sk_switch_model_ok=1
                unset __sk_switch_validated_model
              fi
              if [[ $__sk_switch_action =~ ^(apply|rollback)$ &&
                    $__sk_switch_txid =~ ^[0-9a-f]{32}$ &&
                    $__sk_switch_alias =~ ^[a-z][a-z0-9_-]{0,11}$ &&
                    $__sk_switch_fallback =~ ^[a-z][a-z0-9_-]{0,11}$ &&
                    $__sk_switch_uuid =~ ^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$ &&
                    $__sk_switch_started == "$__sk_started" &&
                    $__sk_switch_shell_pid == "$__sk_shell_pid" &&
                    $__sk_switch_shell_start == "$__sk_shell_start" &&
                    $__sk_switch_model_ok == 1 &&
                    ( -z $__sk_lifecycle_uuid || $__sk_switch_uuid == "$__sk_lifecycle_uuid" ) ]]; then
                # Codex allocates a new thread UUID inside the foreground TUI.
                # Only the request bound to this exact session and shell
                # generation may supply that first-generation identity.
                [[ -n $__sk_lifecycle_uuid ]] || __sk_lifecycle_uuid=$__sk_switch_uuid
                if [[ $__sk_switch_action == apply ]]; then
                  if ! python3 "$__sk_inventory_core" account switch-apply \
                       "$__sk_switch_txid" >/dev/null 2>&1; then
                    python3 "$__sk_inventory_core" account switch-rollback \
                      "$__sk_switch_txid" >/dev/null 2>&1 || true
                    __sk_switch_alias=$__sk_switch_fallback
                  fi
                elif ! python3 "$__sk_inventory_core" account switch-rollback \
                     "$__sk_switch_txid" >/dev/null 2>&1; then
                  __sk_switch_alias=
                fi
                if [[ -n $__sk_switch_alias ]] &&
                   ! python3 "$__sk_inventory_core" account resume-profile \
                     "$__sk_provider" "$__sk_switch_alias" >/dev/null 2>&1 &&
                   [[ $__sk_switch_action == apply ]]; then
                  python3 "$__sk_inventory_core" account switch-rollback \
                    "$__sk_switch_txid" >/dev/null 2>&1 || true
                  __sk_switch_alias=$__sk_switch_fallback
                fi
                if [[ -n $__sk_switch_alias ]] &&
                   python3 "$__sk_inventory_core" account resume-profile \
                     "$__sk_provider" "$__sk_switch_alias" >/dev/null 2>&1; then
                  __sk_switch_tmp="$__sk_start.switch.$$"
                  __sk_switch_expected_tmp="$__sk_expected.switch.$$"
                  __sk_switch_account_tmp="$__sk_account.switch.$$"
                  __sk_switch_launch_tmp=
                  __sk_switch_files=("$__sk_switch_tmp" "$__sk_switch_expected_tmp" \
                    "$__sk_switch_account_tmp")
                  if [[ -n $__sk_switch_model ]]; then
                    __sk_switch_launch_tmp="$__sk_launch.switch.$$"
                    __sk_switch_files+=("$__sk_switch_launch_tmp")
                  fi
                  if [[ -z $__sk_switch_model ||
                        ( ! -e $__sk_launch && ! -L $__sk_launch ) ]]; then
                    ( umask 077
                    printf '%s\t%s\t%s\tresume\n' \
                      "$__sk_provider" "$__sk_cwd" "$__sk_lifecycle_uuid" \
                      > "$__sk_switch_tmp" &&
                    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\tresume\n' \
                      "$__sk_provider" "$__sk_cwd" "$__sk_current_boot_id" \
                      "$__sk_started" "$__sk_shell_pid" "$__sk_shell_start" \
                      "$__sk_daemon_pid" "$__sk_daemon_start" "$__sk_lifecycle_uuid" \
                      > "$__sk_switch_expected_tmp" &&
                    printf '%s\t%s\n' "$__sk_provider" "$__sk_switch_alias" \
                      > "$__sk_switch_account_tmp" &&
                    { [[ -z $__sk_switch_model ]] ||
                      printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
                        "$__sk_provider" "$__sk_cwd" "$__sk_switch_model" \
                        "$__sk_launch_key" "$__sk_current_boot_id" "$__sk_started" \
                        "$__sk_shell_pid" "$__sk_shell_start" "$__sk_daemon_pid" \
                        "$__sk_daemon_start" > "$__sk_switch_launch_tmp"; } &&
                    chmod 600 -- "${__sk_switch_files[@]}" &&
                    mv -- "$__sk_switch_tmp" "$__sk_start" &&
                    mv -- "$__sk_switch_expected_tmp" "$__sk_expected" &&
                    mv -- "$__sk_switch_account_tmp" "$__sk_account" &&
                    { [[ -z $__sk_switch_model ]] ||
                      mv -- "$__sk_switch_launch_tmp" "$__sk_launch"; }
                    ) && __sk_switch_ok=1
                  fi
                  command rm -f -- "$__sk_switch_tmp" "$__sk_switch_expected_tmp" \
                    "$__sk_switch_account_tmp" ${__sk_switch_launch_tmp:+"$__sk_switch_launch_tmp"}
                fi
              fi
            fi
            if (( __sk_switch_ok )); then
              command rm -f -- "$__sk_switch_request"
              # The launch record carrying the model was consumed at first
              # launch, so the shell this `exec` starts would find none and
              # relaunch the provider with no --model at all. Hand the model
              # forward explicitly: a variable set only here, only on this
              # path, so nothing inherited from a parent shell can make an
              # ordinary launch look like a relaunch.
              if [[ -n ${SESSION_KIT_REQUESTED_MODEL:-} ]]; then
                export SESSION_KIT_CARRIED_MODEL=$SESSION_KIT_REQUESTED_MODEL
              else
                unset SESSION_KIT_CARRIED_MODEL
              fi
              exec bash -i
            fi
            echo "[session-kit: account switch could not safely relaunch; request retained]" >&2
          fi
        fi
      fi
    else
      echo "[session-kit: launch record has an unavailable directory; retained for retry]" >&2
    fi
  fi
  unset -f __sk_proc_identity
  unset __sk_start_dir __sk_start __sk_expected __sk_launch __sk_account __sk_arm_attempt
  unset __sk_kit_state
  unset __sk_start_line __sk_expected_line __sk_record_shape_ok __sk_launch_line
  unset __sk_provider __sk_cwd __sk_uuid __sk_launch_mode
  unset __sk_side_provider __sk_side_cwd __sk_side_uuid __sk_side_launch_mode
  unset __sk_boot_id __sk_current_boot_id __sk_inventory_core __sk_started __sk_shell_pid __sk_shell_start __sk_daemon_pid __sk_daemon_start
  unset __sk_generation_ok __sk_launch_mode_ok __sk_walk_pid __sk_walk_depth __sk_walk_ppid __sk_walk_start
  unset __sk_parent_ppid __sk_parent_start
  unset __sk_provider_launched __sk_provider_rc
  unset __sk_lifecycle_uuid __sk_lifecycle_intake __sk_lifecycle_generation
  unset __sk_requested_model __sk_launch_key __sk_launch_provider __sk_launch_cwd
  unset __sk_launch_boot __sk_launch_started __sk_launch_shell_pid __sk_launch_shell_start
  unset __sk_launch_daemon_pid __sk_launch_daemon_start __sk_launch_record_ok __sk_model_args
  unset __sk_launch_model
  unset __sk_account_ok __sk_account_provider __sk_account_alias __sk_account_profile
  unset SESSION_KIT_REQUESTED_MODEL SESSION_KIT_LAUNCH_IDEMPOTENCY_KEY
  unset SESSION_KIT_CARRIED_MODEL

  __sk_lifecycle_event() {
    local __sk_lifecycle_action=$1
    shift
    [[ -n ${__sk_lifecycle_core:-} && -f $__sk_lifecycle_core ]] || return 1
    if [[ $__sk_lifecycle_action == reopen || $__sk_lifecycle_action == closed ]]; then
      SESSION_KIT_LIFECYCLE_SESSION_ID=$__sk_lifecycle_session \
        SESSION_KIT_LIFECYCLE_BOOT_ID=$__sk_lifecycle_boot \
        SESSION_KIT_LIFECYCLE_SHELL_PID=$__sk_lifecycle_shell_pid \
        SESSION_KIT_LIFECYCLE_SHELL_START_TICKS=$__sk_lifecycle_shell_start \
        python3 "$__sk_lifecycle_core" lifecycle "$__sk_lifecycle_action" "$@"
    else
      SESSION_KIT_LIFECYCLE_SESSION_ID=$__sk_lifecycle_session \
        SESSION_KIT_LIFECYCLE_BOOT_ID=$__sk_lifecycle_boot \
        SESSION_KIT_LIFECYCLE_SHELL_PID=$__sk_lifecycle_shell_pid \
        SESSION_KIT_LIFECYCLE_SHELL_START_TICKS=$__sk_lifecycle_shell_start \
        python3 "$__sk_lifecycle_core" lifecycle "$__sk_lifecycle_action" "$@" \
        >/dev/null
    fi
  }

  keep_session() {
    if __sk_lifecycle_event keep on; then
      echo "Automatic cleanup is off for this session."
    else
      echo "Could not mark this session to keep." >&2
      return 1
    fi
  }

  unkeep_session() {
    if __sk_lifecycle_event keep off; then
      echo "This session can be cleaned up again."
    else
      echo "Could not remove the keep marker." >&2
      return 1
    fi
  }

  # `bye` closes this session and says so. It asks nothing (no confirmation
  # anywhere in the kit) and prints no session ID (no screen may print one);
  # the close is recorded as deliberate, so the conversation stays restorable.
  # A directly typed Bash `exit` keeps normal shell semantics, as approved.
  bye() {
    __sk_close_and_report
    printf '\nClosed this session.\n'
    builtin exit
  }

  # A close the PERSON asked for. It happens either way -- refusing to close
  # because a ledger is unwritable would trap them in a session they said they
  # were done with -- but they are TOLD when the conversation did not make it
  # onto the Closed list, because otherwise they walk away believing it is
  # there and find out days later that it is not.
  #
  # All three of these callers used to end with `>/dev/null 2>&1 || true`:
  # the verb's answer was thrown away, and every outcome printed the same
  # confident sentence. That is the same shape as the two blockers this round
  # fixed -- a result saying "this did not work" read as permission -- and it
  # is the half of it that is not destructive, only dishonest.
  __sk_close_and_report() {
    local __sk_close_json=
    [[ -n ${__sk_lifecycle_core:-} && -f $__sk_lifecycle_core ]] || return 0
    # Go through the lifecycle-event contract, but retain and inspect its
    # answer. Closed is the one non-reopen event whose JSON is meaningful to
    # its caller; throwing it away hid a failed ledger write as a safe close.
    __sk_close_json=$(__sk_lifecycle_event closed 2>/dev/null)
    case $__sk_close_json in
      *'"restorable": false'*)
        # Filed, but the list will drop the row: the transcript a restore
        # would read is gone or unreadable. Saying nothing here would leave
        # the person believing Closed sessions can bring it back.
        printf 'Note: this session is closing, but its conversation cannot be read back from this machine, so Closed sessions will not be able to restore it.\n' >&2
        return 1
        ;;
      # Recorded, or a managed shell that never had a conversation to record.
      # Both are the verb working; neither is worth a word.
      *'"recorded": true'* | *'"reason": "no exact conversation"'*) return 0 ;;
      *'"provider": "shell"'*)
        printf 'Note: this session is closing, but its shell-history row could not be added to Closed sessions. No conversation tombstone was written.\n' >&2
        return 1
        ;;
    esac
    printf 'Note: this session is closing, but its conversation could not be added to Closed sessions. It stays in the recovery list instead.\n' >&2
    return 1
  }

  # Handing the window back is a detach, not a close: the session keeps
  # running and the picker that opened this window redraws its list. This is
  # the one detach the kit makes, so every path back to the picker leaves a
  # session in exactly the same state.
  __sk_detach_to_picker() {
    command shpool detach >/dev/null 2>&1
  }

  # Close the session, but ONLY if the CONVERSATION survives the close.
  #
  # `lifecycle closed` has two outcomes and they are not equally good. With an
  # exact conversation it writes the tombstone and a closed-sessions row
  # carrying the uuid, and the conversation can be restored by name. Without
  # one it records a `shell` row instead -- "history only", scrollback and
  # nothing else. Closing a crashed session into that second shape would be
  # WORSE than leaving it open, because the session that still knew the
  # conversation would be gone and the row could never bring it back.
  #
  # --only-with-conversation makes that one decision instead of two steps:
  # either the close happened and the conversation is on the closed list, or
  # nothing at all was written and this session keeps running. Asking without
  # it used to record the history-only row as the answer, which put a closed
  # row on the list for a session that then stayed open.
  #
  # Success here means the session may end. Every other outcome -- a refusal,
  # an unreadable core, a conversation that was never recorded, a session the
  # person marked keep -- keeps it, and records WHY in __sk_close_refusal so
  # the sentence afterwards names the real reason rather than a likely one.
  __sk_close_keeps_the_conversation() {
    local __sk_close_json
    __sk_close_refusal=
    [[ -n ${__sk_lifecycle_core:-} && -f $__sk_lifecycle_core ]] || return 1
    __sk_close_json=$(
      SESSION_KIT_LIFECYCLE_SESSION_ID=$__sk_lifecycle_session \
        SESSION_KIT_LIFECYCLE_BOOT_ID=$__sk_lifecycle_boot \
        SESSION_KIT_LIFECYCLE_SHELL_PID=$__sk_lifecycle_shell_pid \
        SESSION_KIT_LIFECYCLE_SHELL_START_TICKS=$__sk_lifecycle_shell_start \
        python3 "$__sk_lifecycle_core" lifecycle closed \
          --only-with-conversation 2>/dev/null
    )
    case $__sk_close_json in
      *'"reason": "keep is set"'*) __sk_close_refusal=keep ;;
      *'"reason": "the conversation cannot be read back"'*)
        __sk_close_refusal=unreadable ;;
      *'"reason": "the closed-sessions ledger could not be written"'*)
        __sk_close_refusal=unwritable ;;
      *'"reason": "'*'closed-sessions ledger'*)
        __sk_close_refusal=unwritable ;;
    esac
    [[ $__sk_close_json == *'"recorded": true'* ]]
  }

  # One sentence for the session that has to stay. It is printed wherever the
  # kit would rather have closed and could not, because a row that outlives
  # its provider with no explanation is the thing being fixed here: the
  # person is owed the reason and the one action that ends it.
  #
  # Two reasons, two sentences. "You asked me not to" and "I could not promise
  # the conversation back" are different facts, and one standing in for the
  # other is how a person learns to stop believing the screen.
  __sk_say_why_this_session_stays() {
    case ${__sk_close_refusal:-} in
      keep)
        printf 'This one stays open because you asked to keep it. Close it from the picker when you are done; unkeep_session only lets automatic cleanup consider it again later.\n'
        ;;
      reopen-refused)
        printf 'This one stays open because reopening was refused, and a refusal is not proof the conversation is finished. Close it from the picker when you are done with it.\n'
        ;;
      unreadable)
        printf 'This one stays open: its conversation cannot be read back from this machine, so closing it would lose it. Close it from the picker if you are done with it.\n'
        ;;
      unrecorded)
        printf 'Close it from the picker when you are done with it.\n'
        ;;
      unwritable)
        printf 'This one stays open: the Closed sessions list could not be written, so closing it now would leave nothing to restore. Close it from the picker if you are done with it.\n'
        ;;
      *)
        printf 'This one stays open: closing it here could not promise the conversation back. Close it from the picker when you are done with it.\n'
        ;;
    esac
  }

  # Where a session goes when the provider is gone for good AND the close
  # could not keep the conversation. The picker is the answer when this
  # terminal came from it; a managed shell is the honest fallback when the
  # detach cannot run. The session is kept alive here for one reason: without
  # a recorded conversation it is the only thing that still knows which
  # conversation this was, so ending it would lose the thing the operator
  # would want back.
  #
  # When the conversation IS recorded, the caller closes instead — see
  # __sk_close_keeps_the_conversation. Leaving a row behind that says
  # "provider exited" for 72 hours was the operator's complaint (2026-08-15):
  # "the session closes at once and lands in Closed sessions, recoverable,
  # under its REAL name."
  __sk_leave_after_provider_exit() {
    if __sk_detach_to_picker; then
      return 0
    fi
    echo "The picker is not reachable from here. Shell opened; type kit for the picker."
    return 0
  }

  # A crashed provider heals itself instead of asking (D14). The conversation
  # is reopened once, with one line saying so. A second crash within a minute
  # of that reopen is a loop rather than a hiccup, so it stops there and hands
  # the window back to the picker with the session still open and still
  # reopenable. A reopened conversation that ends cleanly closes the session,
  # exactly as a first clean exit does.
  __sk_reopen_after_crash() {
    local __sk_reopen_status __sk_reopen_started __sk_reopen_ended
    while true; do
      printf '\n%s crashed. Reopened.\n' "${__sk_lifecycle_provider^}"
      printf -v __sk_reopen_started '%(%s)T' -1
      __sk_lifecycle_event reopen
      __sk_reopen_status=$?
      if (( __sk_reopen_status == 0 )); then
        # Intent, written before the shell can end: a session closed on
        # purpose must never come back as an unclaimed crash offer. Its
        # answer is read rather than discarded -- see __sk_close_and_report.
        __sk_close_and_report
        printf '\n%s exited. Closing this session.\n' "${__sk_lifecycle_provider^}"
        builtin exit
      fi
      if (( __sk_reopen_status != 76 )); then
        # A REFUSAL NEVER CLOSES. It used to: every status that was not 76
        # was folded into the close path without anyone reading the reason,
        # and one of those reasons is `lifecycle reopen` refusing BECAUSE the
        # trusted inventory says a provider is already running in this
        # terminal. The code proved a provider was alive and then ended the
        # session anyway (found in review, 2026-08-15).
        #
        # There is no safe way to tell the refusals apart from a status alone,
        # and the ones that are ambiguous -- the session list could not be
        # trusted, the daemon generation changed, the recorded exit was not
        # this provider -- are exactly the ones where closing could destroy a
        # conversation. So the rule is now positive rather than residual: a
        # session closes when the reopen RAN and said the conversation is
        # over, and stays for everything else, including any status this code
        # does not recognise. The 72-hour backstop and `k` in the picker both
        # still reach it; nothing is stranded, it just is not ended by a
        # failure that was never evidence of anything.
        echo "Nothing reopened. Back to the picker; this session stays reopenable." >&2
        __sk_close_refusal=reopen-refused
        __sk_say_why_this_session_stays
        __sk_leave_after_provider_exit
        return 0
      fi
      printf -v __sk_reopen_ended '%(%s)T' -1
      if (( __sk_reopen_ended - __sk_reopen_started < 60 )); then
        if __sk_close_keeps_the_conversation; then
          printf '\n%s crashed twice. Closing this session; the conversation is in Closed sessions.\n' \
            "${__sk_lifecycle_provider^}"
          builtin exit
        fi
        printf '\n%s crashed twice. Back to the picker.\n' \
          "${__sk_lifecycle_provider^}"
        __sk_say_why_this_session_stays
        __sk_leave_after_provider_exit
        return 0
      fi
    done
  }

  # THE MARKER. The provider exited and its record could not be written, twice,
  # so nothing here can prove which conversation this was and nothing may close
  # it. This runs down here, and not where the failure was detected, for the
  # dullest possible reason: the functions it needs are defined above this line
  # and below that one.
  if [[ ${__sk_record_unwritable:-0} == 1 ]]; then
    printf '\n[session-kit: %s exited, but its record could not be written, so this session will not close itself.]\n' \
      "${__sk_lifecycle_provider^}" >&2
    __sk_close_refusal=unrecorded
    __sk_say_why_this_session_stays
    __sk_leave_after_provider_exit
  fi
  unset __sk_record_unwritable

  # A CLEAN provider exit is the operator saying "done here", and the session
  # goes with them (operator rule, 2026-08-11, replacing the detach-and-stay-
  # reopenable rule of the same day): the managed shell ends, shpool ends the
  # session with it, and the terminal number returns to the ordinary 7-day
  # quarantine — the same outcome as pressing k on this row from the main
  # menu, reached through the same close this shell has always used rather
  # than a second implementation of it. The exact conversation stays
  # recoverable for its retention window; /kit is the verb for leaving one
  # running. Detaching on a clean exit, and the machine-local marker that used
  # to opt into closing instead, are both gone: one policy, no marker.
  #
  # A CRASH heals itself: the conversation is reopened once, and a second
  # crash inside a minute lands at the picker with this session still open,
  # because a crashed session is the only thing that still knows its exact
  # identity, and nothing about a crash says the operator was finished.
  if [[ ${__sk_provider_exited:-0} == 1 ]]; then
    if (( ${__sk_provider_exit_code:-1} == 0 )); then
      __sk_lifecycle_event user-input >/dev/null 2>&1 || true
      # Intent, written before the shell can end: a session closed on purpose
      # must never come back as an unclaimed crash offer. Its answer is read
      # rather than discarded -- see __sk_close_and_report.
      __sk_close_and_report
      printf '\n%s exited. Closing this session.\n' "${__sk_lifecycle_provider^}"
      builtin exit
    fi
    printf '\n%s exited with status %s.\n' \
      "${__sk_lifecycle_provider^}" "${__sk_provider_exit_code:-unknown}"
    __sk_reopen_after_crash
  fi
  unset __sk_provider_exited __sk_provider_exit_code

  # Local, scoped Needs-you marker. The renderer caches safely and never sends
  # an external notification.
  __sk_waiting() {
    local __sk_prompt_state_root=${XDG_STATE_HOME:-"$HOME/.local/state"}
    local __sk_cache="$__sk_prompt_state_root/session-kit/waiting-count"
    local __sk_now __sk_mtime __sk_age __sk_count
    __sk_now=$(date +%s)
    __sk_mtime=$(stat -c %Y "$__sk_cache" 2>/dev/null || stat -f %m "$__sk_cache" 2>/dev/null || echo 0)
    __sk_age=$((__sk_now - __sk_mtime))
    if (( __sk_age > 20 )); then
      # touch first: even if the probe hangs against a jammed manager, the
      # next 20s of prompts skip spawning more; timeout reaps the straggler.
      (
        umask 077
        mkdir -p "$(dirname "$__sk_cache")"
        touch -- "$__sk_cache"
        if python3 "$HOME/.local/lib/session-kit/current/lib/session_inventory.py" \
          platform timeout 15 -- "$HOME/.local/bin/shpool_status" --waiting-count \
          > "$__sk_cache.new" 2>/dev/null; then
          mv -f -- "$__sk_cache.new" "$__sk_cache"
        else
          rm -f -- "$__sk_cache.new"
        fi
      ) &
    fi
    if [[ -r $__sk_cache ]]; then
      __sk_count=$(command cat -- "$__sk_cache")
    else
      __sk_count=0
    fi
    __sk_count=${__sk_count//[^0-9]/}
    if (( ${__sk_count:-0} > 0 )); then
      printf '\001\e[33m\002●%s\001\e[0m\002 ' "$__sk_count"
    fi
  }
  # The bar at the start of the prompt is this session's colour, the same one
  # the picker draws and the same one the provider window carries. It used to
  # be a constant green: one session, two owners, and a person looking at a
  # green row in the picker and a differently coloured terminal had no way to
  # tell which was lying. The refresh publishes one tiny file per session, so
  # the prompt reads it and forks nothing; an absent file keeps the old green.
  __sk_bar() {
    local __sk_prompt_state_root=${XDG_STATE_HOME:-"$HOME/.local/state"}
    local __sk_color_file="$__sk_prompt_state_root/session-kit/session-color/$SHPOOL_SESSION_NAME"
    local __sk_color_name= __sk_color_code=
    if [[ -r $__sk_color_file ]]; then
      read -r __sk_color_name __sk_color_code < "$__sk_color_file" 2>/dev/null
    fi
    [[ $__sk_color_code =~ ^[0-9]+(\;[0-9]+)*$ ]] || __sk_color_code="38;5;71"
    printf '\001\e[%sm\002▌\001\e[0m\002 ' "$__sk_color_code"
  }
  PS1="\$(__sk_bar)\$(__sk_waiting)$PS1"
fi

unset __sk_state_root __sk_journal_root __sk_session_journal __sk_journal_ready __sk_script_rc

# `kit` opens the session picker on demand (maintainer decision, 2026-08-02:
# SSH lands in a regular shell; the picker only ever appears when typed).
# Works from any plain terminal; inside a managed session it shows the read-only list
# instead, because attaching from inside a session would nest shpool.
kit() {
  if [[ -n ${SHPOOL_SESSION_NAME:-} ]]; then
    "$HOME/.local/bin/sp" list
    printf '  Inside a session. Open others from a new window, or type bye to close this one.\n'
    return 0
  fi
  if [[ -t 0 && -t 1 && -x $HOME/.local/bin/shpool_login_launcher ]]; then
    "$HOME/.local/bin/shpool_login_launcher"
    local __kit_rc=$?
    # 0 = the person left the picker on purpose, whether or not a session was
    # opened first. Anything else is the picker failing to run, and it has
    # already said why on stderr.
    if (( __kit_rc != 0 )); then
      echo "session-kit: the picker could not start." >&2
    fi
    return 0
  fi
  "$HOME/.local/bin/sp" list
}

# SSH lands in a regular shell; one dim static hint, no inventory cost.
if [[ $- == *i* && -n ${SSH_CONNECTION:-} && -z ${SHPOOL_SESSION_NAME:-} && -t 0 ]]; then
  printf '  kit: the picker\n'
fi

# ---- end session-kit shpool integration -------------------------------------
