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

# Copy a private prompt into the paired-launch namespace. The source remains in
# place until the separate audit append succeeds, so an audit failure can never
# erase both copies.
sk_stage_prompt_handoff() {
  local source=$1 destination=$2
  SK_STAGED_PROMPT_IDENTITY=$(python3 - "$source" "$destination" <<'PY'
import os
from pathlib import Path
import stat
import sys

source, destination = map(Path, sys.argv[1:])
if not source.is_absolute():
    source = Path.cwd() / source
flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)

def open_parent(path, *, create=False, private=False):
    if not path.is_absolute() or ".." in path.parts:
        raise OSError("unsafe prompt path")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = path.parent.parts[1:]
        for index, component in enumerate(parts):
            try:
                child = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, 0o700, dir_fd=descriptor)
                os.fsync(descriptor)
                child = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
            info = os.fstat(child)
            if not stat.S_ISDIR(info.st_mode):
                os.close(child)
                raise OSError("unsafe prompt ancestor")
            if private and index == len(parts) - 1 and (
                info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700
            ):
                os.close(child)
                raise OSError("unsafe prompt handoff directory")
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise

source_parent = open_parent(source)
try:
    source_fd = os.open(source.name, flags, dir_fd=source_parent)
except OSError:
    os.close(source_parent)
    raise SystemExit(1)
destination_fd = -1
destination_parent = -1
try:
    info = os.fstat(source_fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
        or not 0 < info.st_size <= 1024 * 1024
    ):
        raise OSError("unsafe prompt")
    payload = os.read(source_fd, info.st_size + 1)
    if len(payload) != info.st_size or b"\0" in payload or b"\x1b" in payload:
        raise OSError("unsafe prompt bytes")
    payload.decode("utf-8")
    destination_parent = open_parent(destination, create=True, private=True)
    create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    create_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    destination_fd = os.open(destination.name, create_flags, 0o600, dir_fd=destination_parent)
    os.fchmod(destination_fd, 0o600)
    written = 0
    while written < len(payload):
        written += os.write(destination_fd, payload[written:])
    os.fsync(destination_fd)
    destination_info = os.fstat(destination_fd)
    if destination_info.st_size != len(payload):
        raise OSError("short prompt write")
    os.fsync(destination_parent)
    path_info = os.stat(source.name, dir_fd=source_parent, follow_symlinks=False)
    if (path_info.st_dev, path_info.st_ino) != (info.st_dev, info.st_ino):
        raise OSError("prompt pathname changed")
    print(f"{info.st_dev}:{info.st_ino}")
except (OSError, UnicodeError):
    try:
        if destination_parent >= 0:
            os.unlink(destination.name, dir_fd=destination_parent)
    except OSError:
        pass
    raise SystemExit(1)
finally:
    if destination_fd >= 0:
        os.close(destination_fd)
    if destination_parent >= 0:
        os.close(destination_parent)
    os.close(source_fd)
    os.close(source_parent)
PY
) || return 1
  [[ $SK_STAGED_PROMPT_IDENTITY =~ ^[0-9]+:[0-9]+$ ]]
}

sk_finalize_prompt_source() {
  local source=$1 identity=$2
  python3 - "$source" "$identity" <<'PY'
import os
from pathlib import Path
import sys

source = Path(sys.argv[1])
expected = tuple(map(int, sys.argv[2].split(":")))
info = source.lstat()
if (info.st_dev, info.st_ino) != expected:
    raise SystemExit(1)
source.unlink()
directory = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY
}

sk_abort_prompt_stage() {
  local source=$1 destination=$2 identity=$3
  python3 - "$source" "$destination" "$identity" <<'PY'
import os
from pathlib import Path
import sys

source, destination = map(Path, sys.argv[1:3])
expected = tuple(map(int, sys.argv[3].split(":")))
try:
    info = source.lstat()
except OSError:
    raise SystemExit(1)
if (info.st_dev, info.st_ino) != expected:
    raise SystemExit(1)
destination.unlink()
directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY
}

sk_write_launch_request() {
  local shpool_id=$1 provider=$2 cwd=$3 model=$4 launch_key=$5
  [[ -n $model && $provider =~ ^(claude|codex)$ && $cwd == /* ]] || return 1
  [[ $model != *[$'\t\r\n\034']* && $launch_key != *[$'\t\r\n\034']* ]] || return 1
  [[ -z $launch_key || $launch_key =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$ ]] || return 1
  [[ ! -e $SK_START_DIR/$shpool_id.launch && ! -L $SK_START_DIR/$shpool_id.launch ]] || return 1
  local temporary="$SK_START_DIR/.${shpool_id}.launch.$$"
  printf '%s\t%s\t%s\t%s\n' "$provider" "$cwd" "$model" "$launch_key" > "$temporary" || return 1
  chmod 600 "$temporary" 2>/dev/null || true
  if ! mv -f -- "$temporary" "$SK_START_DIR/$shpool_id.launch"; then
    [[ ! -e $temporary ]] || command rm -- "$temporary"
    return 1
  fi
}

sk_arm_launch_request() {
  local shpool_id=$1 provider=$2 cwd=$3 model=$4 launch_key=$5
  local boot_id=$6 started=$7 shell_pid=$8 shell_start=$9
  local daemon_pid=${10} daemon_start=${11}
  local path="$SK_START_DIR/$shpool_id.launch"
  [[ -f $path && ! -L $path ]] || return 1
  local request_provider request_cwd request_model request_key extra
  IFS=$'\t' read -r request_provider request_cwd request_model request_key extra < "$path" || return 1
  [[ -z $extra && $request_provider == "$provider" && $request_cwd == "$cwd" &&
     $request_model == "$model" && $request_key == "$launch_key" ]] || return 1
  local temporary="$SK_START_DIR/.${shpool_id}.launch-armed.$$"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$provider" "$cwd" "$model" "$launch_key" "$boot_id" "$started" \
    "$shell_pid" "$shell_start" "$daemon_pid" "$daemon_start" > "$temporary" || return 1
  chmod 600 "$temporary" 2>/dev/null || true
  if ! mv -f -- "$temporary" "$path"; then
    [[ ! -e $temporary ]] || command rm -- "$temporary"
    return 1
  fi
}

sk_quarantine_launch_request() {
  local shpool_id=$1 destination=$2
  local path="$SK_START_DIR/$shpool_id.launch"
  [[ ! -e $path && ! -L $path ]] && return 0
  [[ -f $path && ! -L $path ]] || return 1
  mv -- "$path" "$destination.launch"
}

start_new() {
  sk_require_integration || return 1
  local requested_provider= requested_project= prompt_file= requested_model= launch_key=
  local requested_account= account_profile_json=
  local -a positional=()
  while (($#)); do
    case "$1" in
      --prompt-file)
        [[ -z $prompt_file && -n ${2:-} ]] || {
          sk_die "--prompt-file requires one file"
          return 2
        }
        prompt_file=$2
        shift 2
        ;;
      --model)
        [[ -z $requested_model && -n ${2:-} ]] || {
          sk_die "--model requires one model identifier"
          return 2
        }
        requested_model=$2
        shift 2
        ;;
      --launch-key)
        [[ -z $launch_key && -n ${2:-} ]] || {
          sk_die "--launch-key requires one idempotency key"
          return 2
        }
        launch_key=$2
        shift 2
        ;;
      --account)
        [[ -z $requested_account && -n ${2:-} ]] || {
          sk_die "--account requires one enrolled account alias"
          return 2
        }
        requested_account=$2
        shift 2
        ;;
      --*) sk_die "unknown new-session option: $1"; return 2 ;;
      *) positional+=("$1"); shift ;;
    esac
  done
  (( ${#positional[@]} <= 2 )) || {
    sk_die "new accepts at most a provider and project alias"
    return 2
  }
  requested_provider=${positional[0]:-}
  requested_project=${positional[1]:-}
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

  if [[ -n $prompt_file && $provider != codex ]]; then
    sk_die "--prompt-file is available only for a new Codex session"
    return 2
  fi
  if [[ -n $requested_account ]]; then
    [[ $provider =~ ^(claude|codex)$ &&
       $requested_account =~ ^[a-z][a-z0-9_-]{0,11}$ ]] || {
      sk_die "--account is available only for Claude or Codex with an enrolled alias"
      return 2
    }
    account_profile_json=$(python3 "$INVENTORY_CORE" account launch-profile \
      "$provider" "$requested_account" 2>/dev/null) || {
      sk_die "the selected $provider account is unavailable or no longer matches its login"
      return 1
    }
    python3 -c '
import json,sys
v=json.loads(sys.stdin.read())
raise SystemExit(0 if v.get("provider")==sys.argv[1] and v.get("alias")==sys.argv[2]
                 and isinstance(v.get("profile_dir"),str) and v["profile_dir"].startswith("/") else 1)
' "$provider" "$requested_account" <<<"$account_profile_json" || {
      sk_die "the selected account profile returned an invalid launch identity"
      return 1
    }
  fi
  if [[ -n $requested_model ]]; then
    [[ $provider =~ ^(claude|codex)$ ]] || {
      sk_die "--model is available only for Claude or Codex"
      return 2
    }
    local validated_model
    validated_model=$(python3 "$INVENTORY_CORE" validate-worker-model \
      "$provider" "$requested_model" 2>/dev/null) || {
        sk_die "unsupported or unsafe $provider model identifier"
        return 2
      }
    [[ $validated_model == "$requested_model" ]] || {
      sk_die "model validation did not preserve the exact identifier"
      return 2
    }
  elif [[ -n $launch_key ]]; then
    sk_die "--launch-key requires --model"
    return 2
  fi
  if [[ -n $launch_key ]]; then
    launch_key=$(python3 - "$launch_key" <<'PY'
import re
import sys
value = sys.argv[1].strip()
if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) is None:
    raise SystemExit(1)
print(value)
PY
    ) || {
      sk_die "--launch-key is not a supported idempotency key"
      return 2
    }
  fi

  local launch_uuid= launch_mode=new
  if [[ $provider == claude && -z $requested_account ]] && sk_prebake_claude; then
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
  local prompt_handoff=
  if [[ -n $prompt_file ]]; then
    prompt_handoff=$SK_START_DIR/$id.prompt
    if ! sk_stage_prompt_handoff "$prompt_file" "$prompt_handoff"; then
      sk_lock_release 9
      sk_die "prompt file must be a readable owner-private mode-0600 regular UTF-8 file"
      sk_log_action prompt_handoff refused || true
      return 1
    fi
    if ! sk_log_action prompt_handoff created; then
      sk_abort_prompt_stage "$prompt_file" "$prompt_handoff" \
        "$SK_STAGED_PROMPT_IDENTITY" >/dev/null 2>&1 || true
      sk_lock_release 9
      sk_die "prompt handoff could not be audited; no session was created and at least one private copy was retained"
      return 1
    fi
  fi
  sk_write_start_record "$id" "$provider" "$cwd" "$launch_uuid" "$launch_mode" || {
    [[ -z $prompt_handoff ]] ||
      sk_abort_prompt_stage "$prompt_file" "$prompt_handoff" \
        "$SK_STAGED_PROMPT_IDENTITY" >/dev/null 2>&1 || true
    sk_lock_release 9
    return 1
  }
  if [[ -n $requested_account ]] &&
     ! sk_write_account_record "$id" "$provider" "$requested_account"; then
    command rm -f -- "$SK_START_DIR/$id" "$SK_START_DIR/$id.account"
    [[ -z $prompt_handoff ]] ||
      sk_abort_prompt_stage "$prompt_file" "$prompt_handoff" \
        "$SK_STAGED_PROMPT_IDENTITY" >/dev/null 2>&1 || true
    sk_lock_release 9
    sk_die "selected account launch record could not be written; no session was created"
    return 1
  fi
  if [[ -n $requested_model ]] &&
     ! sk_write_launch_request "$id" "$provider" "$cwd" "$requested_model" "$launch_key"; then
    command rm -f -- "$SK_START_DIR/$id" "$SK_START_DIR/$id.account"
    [[ -z $prompt_handoff ]] ||
      sk_abort_prompt_stage "$prompt_file" "$prompt_handoff" \
        "$SK_STAGED_PROMPT_IDENTITY" >/dev/null 2>&1 || true
    sk_lock_release 9
    sk_die "requested model launch record could not be written; no session was created"
    return 1
  fi
  if [[ -n $prompt_handoff ]] &&
     ! sk_finalize_prompt_source "$prompt_file" "$SK_STAGED_PROMPT_IDENTITY"; then
    sk_log_action prompt_handoff source-retained >/dev/null 2>&1 || true
  fi
  printf 'Starting a %s session in %s\n' "$provider" "$cwd"
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
    [[ -z $requested_model || -z $quarantine ]] ||
      sk_quarantine_launch_request "$id" "$quarantine" >/dev/null 2>&1 || true
    sk_lock_release 9
    sk_die "shpool did not confirm session creation; launch record quarantined at ${quarantine:-$SK_START_DIR/failed}"
    return 1
  fi
  # A timed-out reply (124/137) does not prove the session was NOT created —
  # generation capture below settles it either way.
  if ! sk_capture_session_generation "$id" "$creation_floor_ms"; then
    local quarantine
    quarantine=$(sk_quarantine_start_record "$id" generation-unproven 2>/dev/null || true)
    [[ -z $requested_model || -z $quarantine ]] ||
      sk_quarantine_launch_request "$id" "$quarantine" >/dev/null 2>&1 || true
    sk_lock_release 9
    sk_die "the new session may be open, but its exact generation was not proven; unarmed launch record quarantined at ${quarantine:-$SK_START_DIR/failed}"
    return 1
  fi
  local created_started=$SK_CREATED_STARTED
  local created_boot_id=$SK_CREATED_BOOT_ID
  local created_shell_pid=$SK_CREATED_SHELL_PID created_shell_start=$SK_CREATED_SHELL_START
  local created_daemon_pid=$SK_CREATED_DAEMON_PID created_daemon_start=$SK_CREATED_DAEMON_START
  if [[ -n $requested_model ]] &&
     ! sk_arm_launch_request "$id" "$provider" "$cwd" "$requested_model" "$launch_key" \
       "$created_boot_id" "$created_started" "$created_shell_pid" "$created_shell_start" \
       "$created_daemon_pid" "$created_daemon_start"; then
    local quarantine
    quarantine=$(sk_quarantine_start_record "$id" model-arming-failed 2>/dev/null || true)
    [[ -z $quarantine ]] ||
      sk_quarantine_launch_request "$id" "$quarantine" >/dev/null 2>&1 || true
    sk_lock_release 9
    sk_die "requested model could not be bound to the exact session generation; launch record quarantined at ${quarantine:-$SK_START_DIR/failed}"
    return 1
  fi
  sk_write_generation_record "$id" "$provider" "$cwd" "$launch_uuid" \
    "$created_boot_id" "$created_started" "$created_shell_pid" "$created_shell_start" \
    "$created_daemon_pid" "$created_daemon_start" "$launch_mode" || {
      local quarantine
      quarantine=$(sk_quarantine_start_record "$id" arming-failed 2>/dev/null || true)
      [[ -z $requested_model || -z $quarantine ]] ||
        sk_quarantine_launch_request "$id" "$quarantine" >/dev/null 2>&1 || true
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
    sk_die "the new session is open, but its completed shell launch record could not be cleared"
    return 1
  fi
  if [[ $provider != shell ]] && ! sk_wait_for_provider "$id" "$provider" "$cwd" \
    "$launch_uuid" \
    "$created_boot_id" "$created_started" "$created_shell_pid" "$created_shell_start" \
    "$created_daemon_pid" "$created_daemon_start" "$launch_mode"; then
    sk_die "the new session remains open, but exact $provider startup was not proven; launch record retained. Open it from the picker and run 'exec bash -i' to retry"
    return 1
  fi
  if [[ $provider != shell && -n ${SK_PROVEN_UUID:-} ]]; then
    if [[ -n $requested_account ]] &&
       ! python3 "$INVENTORY_CORE" account bind "$provider" "$SK_PROVEN_UUID" \
         "$requested_account" --source launch >/dev/null; then
      sk_die "the session is open, but its verified account binding could not be recorded"
      return 1
    fi
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
  local provider=$1 uuid=$2 cwd=$3 account_alias=${4:-}
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
  if [[ -n $account_alias ]]; then
    [[ $account_alias =~ ^[a-z][a-z0-9_-]{0,11}$ ]] || {
      sk_die "restore account alias is invalid"
      return 1
    }
    python3 "$INVENTORY_CORE" account resume-profile "$provider" "$account_alias" \
      >/dev/null 2>&1 || {
      sk_die "restore account profile is unavailable or does not match its login"
      return 1
    }
  fi

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
    sk_die "that exact conversation is already active"
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
  if [[ -n $account_alias ]] &&
     ! sk_write_account_record "$id" "$provider" "$account_alias"; then
    command rm -f -- "$SK_START_DIR/$id"
    sk_lock_release 9
    sk_die "restore account launch record could not be written; nothing launched"
    return 1
  fi
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
    sk_die "the recovery shell may be open, but its exact generation was not proven; unarmed record quarantined at ${quarantine:-$SK_START_DIR/failed}"
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
    sk_die "the recovered session remains open, but exact $provider recovery was not proven; launch record retained. Open it from the picker and run 'exec bash -i' to retry"
    return 1
  fi
  if [[ -n $account_alias ]] &&
     ! python3 "$INVENTORY_CORE" account bind "$provider" "$uuid" "$account_alias" \
       --source restore >/dev/null; then
    sk_die "the exact conversation was restored, but its account binding could not be recorded"
    return 1
  fi
  printf '%s\n' "$id"
}
