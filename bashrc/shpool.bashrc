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
    # An exhausted wait means sp died between writing the start record and
    # arming it. Every later shell in this session would silently stall 3s
    # here; say it once instead.
    if [[ ! -r $__sk_expected ]]; then
      echo "[session-kit: launch record incomplete; starting a plain shell]"
    fi
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
        __sk_provider_launched=0
        __sk_provider_exited=0
        __sk_provider_rc=0
        case "$__sk_provider" in
        claude)
          if ! command -v claude >/dev/null 2>&1; then
            echo "[session-kit: Claude is unavailable; launch record retained for retry]" >&2
          elif [[ $__sk_launch_mode != new ]] || \
              __sk_new_claude_uuid=$(python3 -c 'import uuid; print(uuid.uuid4())'); then
            command rm -- "$__sk_start" "$__sk_expected"
            __sk_provider_launched=1
            case "$__sk_launch_mode" in
              new) claude --session-id "$__sk_new_claude_uuid"; __sk_provider_rc=$? ;;
              resume) claude --resume "$__sk_uuid"; __sk_provider_rc=$? ;;
              fork) claude --resume "$__sk_uuid" --fork-session; __sk_provider_rc=$? ;;
            esac
          else
            echo "[session-kit: Claude session identity could not be allocated; launch record retained for retry]" >&2
          fi
          unset __sk_new_claude_uuid
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
            # A private per-repository coordination config can opt future Codex
            # sessions into a local App Server plus an exact-thread broker.
            # Existing direct sessions are never changed. Invalid, unsafe, or
            # absent config fails back to the normal direct TUI launch.
            __sk_coord_broker=
            __sk_coord_config="$HOME/.config/session-kit/coordination.json"
            if [[ -f $__sk_coord_config && ! -L $__sk_coord_config ]]; then
              __sk_coord_broker=$(python3 - "$__sk_coord_config" "$__sk_cwd" <<'PY'
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
        or os.path.realpath(launch_cwd) != repo_root
        or not os.path.isabs(broker)
        or not os.path.isfile(broker)
        or os.path.islink(broker)
    ):
        raise ValueError("inactive config")
    broker_metadata = os.stat(broker)
    if broker_metadata.st_uid != os.geteuid() or broker_metadata.st_mode & 0o022:
        raise ValueError("unsafe broker")
    print(broker)
except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
    pass
PY
              )
            fi
            __sk_codex_remote=()
            __sk_app_server_pid=
            __sk_broker_pid=
            __sk_app_socket=
            if [[ -n $__sk_coord_broker ]]; then
              __sk_app_dir="$__sk_state_root/session-kit/app-server/$SHPOOL_SESSION_NAME"
              if mkdir -p -- "$__sk_app_dir" && chmod 700 -- "$__sk_app_dir"; then
                __sk_app_socket="$__sk_app_dir/app.sock"
                __sk_app_log="$__sk_app_dir/app-server.log"
                if [[ ! -e $__sk_app_socket ]]; then
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
                    __sk_broker_args=(
                      --socket "$__sk_app_socket"
                      --repo "$__sk_cwd"
                      --server-pid "$__sk_app_server_pid"
                    )
                    [[ $__sk_launch_mode == resume && -n $__sk_uuid ]] && \
                      __sk_broker_args+=(--thread "$__sk_uuid")
                    python3 "$__sk_coord_broker" "${__sk_broker_args[@]}" \
                      >>"$__sk_app_dir/broker.log" 2>&1 &
                    __sk_broker_pid=$!
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
            command rm -- "$__sk_start" "$__sk_expected"
            __sk_provider_launched=1
            # A new-mode Codex boots before any thread title can exist, so its
            # status bar shows the conversation ID for that process's life.
            # The marker lets the picker request one safe provider bounce
            # (codex resume of the same conversation) once a title exists.
            if [[ $__sk_launch_mode == new ]]; then
              ( umask 077
                mkdir -p "$__sk_state_root/session-kit/provider-untitled" &&
                  : > "$__sk_state_root/session-kit/provider-untitled/$SHPOOL_SESSION_NAME"
              ) 2>/dev/null || true
            fi
            while :; do
              __sk_codex_theme=()
              __sk_theme_color=
              if [[ -f $__sk_inventory_core ]]; then
                if [[ -n $__sk_uuid ]]; then
                  __sk_theme_color=$(python3 "$__sk_inventory_core" color effective codex "$__sk_uuid" 2>/dev/null || true)
                else
                  __sk_theme_color=$(python3 "$__sk_inventory_core" color launch-pick "$SHPOOL_SESSION_NAME" 2>/dev/null || true)
                fi
              fi
              case "$__sk_theme_color" in
                red|blue|green|yellow|purple|orange|pink|cyan)
                  if [[ -r ${CODEX_HOME:-$HOME/.codex}/themes/sk-$__sk_theme_color.tmTheme ]]; then
                    __sk_codex_theme=(-c "tui.theme=\"sk-$__sk_theme_color\"")
                  fi
                  ;;
              esac
              unset __sk_theme_color
              case "$__sk_launch_mode" in
                new) codex "${__sk_codex_no_update[@]}" "${__sk_codex_theme[@]}" "${__sk_codex_remote[@]}" --no-alt-screen; __sk_provider_rc=$? ;;
                resume) codex "${__sk_codex_no_update[@]}" "${__sk_codex_theme[@]}" "${__sk_codex_remote[@]}" --no-alt-screen resume "$__sk_uuid"; __sk_provider_rc=$? ;;
                fork) codex "${__sk_codex_no_update[@]}" "${__sk_codex_theme[@]}" "${__sk_codex_remote[@]}" --no-alt-screen fork "$__sk_uuid"; __sk_provider_rc=$? ;;
              esac
              # A kit-requested bounce relaunches the SAME conversation once,
              # so the fresh process boots with its title and theme. Any other
              # exit falls through to the normal provider-exit handling.
              __sk_bounce="$__sk_state_root/session-kit/provider-bounce/$SHPOOL_SESSION_NAME"
              if [[ -f $__sk_bounce && ! -L $__sk_bounce ]]; then
                __sk_bounce_uuid=$(command head -c 64 -- "$__sk_bounce" 2>/dev/null | tr -cd '0-9a-fA-F-')
                command rm -f -- "$__sk_bounce"
                if [[ $__sk_bounce_uuid =~ ^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$ ]]; then
                  __sk_uuid=$__sk_bounce_uuid
                  __sk_launch_mode=resume
                  unset __sk_bounce_uuid __sk_bounce
                  continue
                fi
                unset __sk_bounce_uuid
              fi
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
             python3 "$__sk_lifecycle_core" lifecycle provider-exited >/dev/null
          then
            __sk_provider_exited=1
            printf '\n%s exited with status %s. This terminal is still open.\n' \
              "${__sk_provider^}" "$__sk_provider_rc"
          else
            echo "[session-kit: provider exited; lifecycle proof is unavailable, so automatic cleanup is blocked]" >&2
          fi
        fi
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
  unset __sk_provider_launched __sk_provider_rc

  __sk_lifecycle_event() {
    local __sk_lifecycle_action=$1
    shift
    [[ -n ${__sk_lifecycle_core:-} && -f $__sk_lifecycle_core ]] || return 1
    if [[ $__sk_lifecycle_action == reopen ]]; then
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
      echo "Automatic cleanup is disabled for this terminal."
    else
      echo "Could not mark this terminal to keep." >&2
      return 1
    fi
  }

  unkeep_session() {
    if __sk_lifecycle_event keep off; then
      echo "Automatic cleanup may resume after every safety condition is met."
    else
      echo "Could not remove the keep marker." >&2
      return 1
    fi
  }

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

  __sk_provider_exit_menu() {
    local __sk_menu_choice
    while true; do
      printf '\nProvider exited: [r] reopen conversation  [k] keep terminal  [s] shell  [c] close\n'
      if ! IFS= read -r -p "Choice: " __sk_menu_choice; then
        # EOF or a terminal signal is user activity. Closing is safe even when
        # the private input marker cannot be written.
        __sk_lifecycle_event user-input >/dev/null 2>&1 || true
        builtin exit
      fi
      __sk_menu_choice=${__sk_menu_choice,,}
      if [[ $__sk_menu_choice == c || $__sk_menu_choice == close ]]; then
        __sk_lifecycle_event user-input >/dev/null 2>&1 || true
        builtin exit
      fi
      if ! __sk_lifecycle_event user-input >/dev/null 2>&1; then
        echo "Choice not applied: Session Kit could not record terminal use. Close remains available." >&2
        continue
      fi
      case "$__sk_menu_choice" in
        r|reopen)
          if ! __sk_lifecycle_event reopen; then
            echo "Exact recovery is not ready. Choose shell to use the provider directly, or close and reopen from the dashboard." >&2
          fi
          ;;
        k|keep)
          keep_session
          ;;
        s|shell)
          echo "Shell opened. This terminal is permanently excluded from automatic cleanup."
          return 0
          ;;
        *)
          echo "Unknown choice. Use r, k, s, or c."
          ;;
      esac
    done
  }

  if [[ ${__sk_provider_exited:-0} == 1 ]]; then
    __sk_provider_exit_menu
  fi
  unset __sk_provider_exited

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
  PS1="\[\e[38;5;71m\]▌\[\e[0m\] \$(__sk_waiting)$PS1"
fi

unset __sk_state_root __sk_journal_root __sk_session_journal __sk_journal_ready __sk_script_rc

# `kit` opens the session picker on demand (Dan 2026-08-02: SSH lands in a
# regular shell; the picker only ever appears when typed). Works from any
# plain terminal; inside a managed session it shows the read-only list
# instead, because attaching from inside a session would nest shpool.
kit() {
  if [[ -n ${SHPOOL_SESSION_NAME:-} ]]; then
    "$HOME/.local/bin/sp" list
    printf '  (inside a managed session — open others from a new window, or bye to leave)\n'
    return 0
  fi
  if [[ -t 0 && -t 1 && -x $HOME/.local/bin/shpool_login ]]; then
    "$HOME/.local/bin/shpool_login"
    local __kit_rc=$?
    # 0 = a session was attached and ended; 2 = chose the terminal. Both are
    # normal returns to this shell.
    if (( __kit_rc != 0 && __kit_rc != 2 )); then
      echo "[session picker unavailable; still in the plain shell]"
    fi
    return 0
  fi
  "$HOME/.local/bin/sp" list
}

# SSH lands in a regular shell; one dim static hint, no inventory cost.
if [[ $- == *i* && -n ${SSH_CONNECTION:-} && -z ${SHPOOL_SESSION_NAME:-} && -t 0 ]]; then
  printf '  kit: open sessions\n'
fi
# ---- end session-kit shpool integration -------------------------------------
