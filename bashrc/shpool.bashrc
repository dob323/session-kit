# ---- session-kit shpool integration -----------------------------------------
# Source this block from ~/.bashrc. Installed releases keep this file immutable.

export PATH="$HOME/.cargo/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

# Keep AI TUI output in normal terminal history. The session journal is the
# reconnect source; provider-native transcripts remain the conversation source.
export CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1

__sk_state_root=${XDG_STATE_HOME:-"$HOME/.local/state"}
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
    if [[ ${SESSION_KIT_MACOS_PREVIEW:-0} != 1 ]]; then
      echo "[session-kit: macOS preview is disabled; continuing without capture]" >&2
      export SHPOOL_JOURNAL=disabled
      exec bash -i
    fi
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
  __sk_start_dir=${SESSION_KIT_START_DIR:-"$__sk_state_root/shpool-start"}
  __sk_start="$__sk_start_dir/$SHPOOL_SESSION_NAME"
  __sk_expected="$__sk_start.expected"
  if [[ -n ${SHPOOL_JOURNAL:-} && -r $__sk_start ]]; then
    # The shell can start before `sp` has captured its exact generation. Wait
    # only for an atomically written sidecar; an unarmed or stale main record
    # never launches a provider.
    for __sk_arm_attempt in {1..30}; do
      [[ -r $__sk_expected ]] && break
      sleep 0.1
    done
  fi
  if [[ -n ${SHPOOL_JOURNAL:-} && -r $__sk_start && -r $__sk_expected ]]; then
    __sk_provider= __sk_cwd= __sk_uuid= __sk_launch_mode=
    __sk_side_provider= __sk_side_cwd= __sk_side_uuid= __sk_side_launch_mode=
    __sk_boot_id= __sk_started= __sk_shell_pid= __sk_shell_start=
    __sk_daemon_pid= __sk_daemon_start=
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
    __sk_inventory_core=${SESSION_KIT_INVENTORY_CORE:-${SESSION_KIT_RELEASE_DIR:-}/lib/session_inventory.py}
    if [[ -n ${SESSION_KIT_BOOT_ID_FILE:-} ]]; then
      __sk_current_boot_id=$(command cat -- "$SESSION_KIT_BOOT_ID_FILE" 2>/dev/null || true)
    elif [[ $(uname -s 2>/dev/null) == Darwin && ${SESSION_KIT_MACOS_PREVIEW:-0} == 1 &&
            -f $__sk_inventory_core ]]; then
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
        [[ ${SESSION_KIT_MACOS_PREVIEW:-0} == 1 && -f $__sk_inventory_core ]] ||
          return 1
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
    if (( __sk_record_shape_ok && __sk_launch_mode_ok )) &&
       [[ $__sk_provider == "$__sk_side_provider" &&
          $__sk_cwd == "$__sk_side_cwd" &&
          $__sk_uuid == "$__sk_side_uuid" &&
          $__sk_launch_mode == "$__sk_side_launch_mode" &&
          -n $__sk_boot_id && $__sk_boot_id == "$__sk_current_boot_id" &&
          $__sk_started =~ ^[0-9]+$ &&
          $__sk_shell_pid =~ ^[0-9]+$ && $__sk_shell_start =~ ^[0-9]+$ &&
          $__sk_daemon_pid =~ ^[0-9]+$ && $__sk_daemon_start =~ ^[0-9]+$ ]]; then
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
    if (( ! __sk_generation_ok )); then
      echo "[session-kit: stale or mismatched launch record retained; provider not started]" >&2
    elif [[ $__sk_cwd == /* && -d $__sk_cwd ]]; then
      if ! cd -- "$__sk_cwd"; then
        echo "[session-kit: launch directory is unavailable; launch record retained for retry]" >&2
      else
        case "$__sk_provider" in
        claude)
          if ! command -v claude >/dev/null 2>&1; then
            echo "[session-kit: Claude is unavailable; launch record retained for retry]" >&2
          else
            case "$__sk_launch_mode" in
              new) exec claude ;;
              resume) exec claude --resume "$__sk_uuid" ;;
              fork) exec claude --resume "$__sk_uuid" --fork-session ;;
            esac
          fi
          ;;
        codex)
          if ! command -v codex >/dev/null 2>&1; then
            echo "[session-kit: Codex is unavailable; launch record retained for retry]" >&2
          else
            # A managed session is launched with nobody watching it. Codex's
            # startup upgrade prompt blocks on a keypress that never comes, so
            # the session sits at "setup incomplete" forever and its
            # conversation never loads. Suppressed explicitly here rather than
            # relying on user config.
            __sk_codex_no_update=(-c check_for_update_on_startup=false)
            case "$__sk_launch_mode" in
              new) exec codex "${__sk_codex_no_update[@]}" --no-alt-screen ;;
              resume) exec codex "${__sk_codex_no_update[@]}" --no-alt-screen resume "$__sk_uuid" ;;
              fork) exec codex "${__sk_codex_no_update[@]}" --no-alt-screen fork "$__sk_uuid" ;;
            esac
          fi
          ;;
        shell)
          command rm -- "$__sk_start" "$__sk_expected"
          ;;
        esac
      fi
    else
      echo "[session-kit: launch record has an unavailable directory; retained for retry]" >&2
    fi
  fi
  unset -f __sk_proc_identity
  unset __sk_start_dir __sk_start __sk_expected __sk_arm_attempt
  unset __sk_start_line __sk_expected_line __sk_record_shape_ok
  unset __sk_provider __sk_cwd __sk_uuid __sk_launch_mode
  unset __sk_side_provider __sk_side_cwd __sk_side_uuid __sk_side_launch_mode
  unset __sk_boot_id __sk_current_boot_id __sk_inventory_core __sk_started __sk_shell_pid __sk_shell_start __sk_daemon_pid __sk_daemon_start
  unset __sk_generation_ok __sk_launch_mode_ok __sk_walk_pid __sk_walk_depth __sk_walk_ppid __sk_walk_start
  unset __sk_parent_ppid __sk_parent_start

  # `bye` is the convenient confirmed close. A directly typed Bash `exit`
  # retains normal shell semantics, as approved.
  bye() {
    local __sk_answer
    printf '\nClose shpool session %s?\n' "$SHPOOL_SESSION_NAME"
    read -r -p "Type the exact ID '$SHPOOL_SESSION_NAME' to confirm: " __sk_answer
    if [[ $__sk_answer == "$SHPOOL_SESSION_NAME" ]]; then
      builtin exit
    fi
    echo "Close cancelled."
  }

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
      (
        umask 077
        mkdir -p "$(dirname "$__sk_cache")"
        "$HOME/.local/bin/shpool_status" --waiting-count > "$__sk_cache" 2>/dev/null
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
  PS1="\[\e[38;5;71m\]▌\[\e[0m\] \$(__sk_waiting)$PS1"
fi

unset __sk_state_root __sk_journal_root __sk_session_journal __sk_journal_ready __sk_script_rc

# Auto-open the read-only picker only for interactive SSH terminals.
# Escape hatch: touch ~/.no_shpool.
if [[ $- == *i* && -n ${SSH_CONNECTION:-} && -z ${SHPOOL_SESSION_NAME:-} && -t 0 \
      && ! -e $HOME/.no_shpool && -x $HOME/.local/bin/shpool_login ]]; then
  "$HOME/.local/bin/shpool_login"
  __sk_rc=$?
  if [[ $__sk_rc -eq 0 ]]; then builtin exit; fi
  if [[ $__sk_rc -ne 2 ]]; then
    echo "[session picker unavailable; continuing in a plain shell]"
  fi
  unset __sk_rc
fi
# ---- end session-kit shpool integration -------------------------------------
