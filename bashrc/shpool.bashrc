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

# Run one private handoff as a headless Codex turn. The prompt is an already
# open file descriptor, never an argv value or terminal byte stream. The
# trusted UserPromptSubmit hook first records provider acceptance, then durable
# intake. This launcher removes the handoff only after the exact intake commit
# exists. Once a provider process starts, every uncommitted outcome is moved to
# a Needs You quarantine before its terminal record is written and is never
# submitted to the provider again.
__sk_codex_exec_handoff() {
  local __sk_handoff=$1 __sk_acceptance=$2 __sk_intake=$3 __sk_completion=$4
  shift 4
  python3 - "$__sk_handoff" "$__sk_acceptance" "$__sk_intake" "$__sk_completion" "$@" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time

handoff = Path(sys.argv[1])
acceptance = Path(sys.argv[2])
intake = Path(sys.argv[3])
completion = Path(sys.argv[4])
quarantine = Path(f"{handoff}.quarantined")
intake_pending = Path(f"{handoff}.intake_pending")
command = sys.argv[5:]
managed_generation = os.environ.get("SESSION_KIT_MANAGED_GENERATION", "")
uuid_re = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
sha_re = re.compile(r"[0-9a-f]{64}")
if not command or command[-2:] != ["exec", "-"]:
    raise SystemExit(2)
if (
    not handoff.is_absolute()
    or acceptance != Path(f"{handoff}.accepted")
    or intake != Path(f"{handoff}.intake_committed")
    or completion != Path(f"{handoff}.completed")
    or not managed_generation
):
    raise SystemExit(2)

def reject_duplicates(pairs):
    value = {}
    for key, member in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = member
    return value

def open_parent(path):
    if not path.is_absolute() or ".." in path.parts:
        raise OSError("unsafe prompt path")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        components = path.parent.parts[1:]
        for index, component in enumerate(components):
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            info = os.fstat(child)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid not in {0, os.geteuid()}
                or info.st_mode & 0o022
                or (index == len(components) - 1 and info.st_uid != os.geteuid())
            ):
                os.close(child)
                raise OSError("unsafe prompt directory ancestry")
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise

parent = open_parent(handoff)
flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
try:
    descriptor = os.open(handoff.name, flags, dir_fd=parent)
except OSError:
    os.close(parent)
    raise SystemExit(1)
try:
    info = os.fstat(descriptor)
    payload = os.read(descriptor, 1024 * 1024 + 1)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
        or not payload
        or len(payload) > 1024 * 1024
        or b"\0" in payload
    ):
        raise OSError("unsafe prompt handoff")
    payload.decode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    parent_info = os.fstat(parent)
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or stat.S_ISLNK(parent_info.st_mode)
        or parent_info.st_uid != os.geteuid()
        or stat.S_IMODE(parent_info.st_mode) != 0o700
    ):
        raise OSError("unsafe prompt handoff directory")

    def atomic_record(path, record):
        encoded = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        if path.parent != handoff.parent:
            raise OSError("terminal record escaped handoff directory")
        temporary_descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            os.fchmod(temporary_descriptor, 0o600)
            with os.fdopen(temporary_descriptor, "wb") as stream:
                temporary_descriptor = -1
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(Path(temporary).name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            if temporary_descriptor >= 0:
                os.close(temporary_descriptor)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def exact_acceptance():
        try:
            accepted_fd = os.open(acceptance.name, flags, dir_fd=parent)
            accepted_info = os.fstat(accepted_fd)
            raw = os.read(accepted_fd, 65537)
            os.close(accepted_fd)
            record = json.loads(raw, object_pairs_hook=reject_duplicates)
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return False
        valid = (
            stat.S_ISREG(accepted_info.st_mode)
            and not stat.S_ISLNK(accepted_info.st_mode)
            and accepted_info.st_uid == os.geteuid()
            and stat.S_IMODE(accepted_info.st_mode) == 0o600
            and isinstance(record, dict)
            and set(record) == {
                "bytes", "schema_version", "session_id", "sha256", "status", "turn_id"
            }
            and record["schema_version"] == 1
            and record["status"] == "accepted"
            and record["bytes"] == len(payload)
            and record["sha256"] == digest
            and isinstance(record["session_id"], str)
            and uuid_re.fullmatch(record["session_id"]) is not None
            and isinstance(record["turn_id"], str)
            and uuid_re.fullmatch(record["turn_id"]) is not None
        )
        return record if valid else None

    def exact_intake(accepted):
        try:
            intake_fd = os.open(intake.name, flags, dir_fd=parent)
            intake_info = os.fstat(intake_fd)
            raw = os.read(intake_fd, 65537)
            os.close(intake_fd)
            record = json.loads(raw, object_pairs_hook=reject_duplicates)
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError, ValueError):
            return False
        return (
            stat.S_ISREG(intake_info.st_mode)
            and intake_info.st_uid == os.geteuid()
            and stat.S_IMODE(intake_info.st_mode) == 0o600
            and isinstance(record, dict)
            and set(record) == {
                "schema_version", "status", "provider", "session_id",
                "submission_key", "prompt_sha256", "bytes", "source_event_id",
                "intake_msg_id", "requirements_revision", "requirements_digest",
                "managed_generation", "committed_unix_ms",
            }
            and record["schema_version"] == 2
            and record["status"] == "intake_committed"
            and record["provider"] == "codex"
            and accepted
            and record["session_id"] == accepted["session_id"]
            and record["submission_key"] == accepted["turn_id"]
            and record["prompt_sha256"] == digest
            and record["bytes"] == len(payload)
            and isinstance(record["source_event_id"], str)
            and sha_re.fullmatch(record["source_event_id"]) is not None
            and isinstance(record["intake_msg_id"], str)
            and bool(record["intake_msg_id"])
            and isinstance(record["requirements_revision"], int)
            and not isinstance(record["requirements_revision"], bool)
            and record["requirements_revision"] >= 0
            and isinstance(record["requirements_digest"], str)
            and sha_re.fullmatch(record["requirements_digest"]) is not None
            and record["managed_generation"] == managed_generation
            and isinstance(record["committed_unix_ms"], int)
            and not isinstance(record["committed_unix_ms"], bool)
            and record["committed_unix_ms"] > 0
        )

    def failpoint_armed(name):
        # Crashes this process at an exact instruction so a test can prove the
        # handoff cannot replay. Gated on SESSION_KIT_TESTING because an
        # ungated failpoint lets any process that can set an environment
        # variable kill every Codex prompt handoff on the machine.
        return (
            os.environ.get("SESSION_KIT_TESTING") == "1"
            and os.environ.get("SESSION_KIT_TEST_FAILPOINT") == name
        )

    def consume():
        path_info = os.stat(handoff.name, dir_fd=parent, follow_symlinks=False)
        if (path_info.st_dev, path_info.st_ino) != (info.st_dev, info.st_ino):
            raise OSError("prompt handoff pathname changed")
        os.unlink(handoff.name, dir_fd=parent)
        os.fsync(parent)

    def refuse_prior_terminal():
        try:
            completed_fd = os.open(completion.name, flags, dir_fd=parent)
            completed_info = os.fstat(completed_fd)
            raw = os.read(completed_fd, 65537)
            os.close(completed_fd)
            record = json.loads(raw, object_pairs_hook=reject_duplicates)
        except FileNotFoundError:
            return
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            raise OSError("unsafe prompt completion record")
        if (
            not stat.S_ISREG(completed_info.st_mode)
            or completed_info.st_uid != os.geteuid()
            or stat.S_IMODE(completed_info.st_mode) != 0o600
            or not isinstance(record, dict)
            or set(record) != {
                "exit_code", "managed_generation", "schema_version", "sha256", "status"
            }
            or record["schema_version"] != 3
            or record["sha256"] != digest
            or record["managed_generation"] != managed_generation
            or not isinstance(record["exit_code"], int)
            or isinstance(record["exit_code"], bool)
            or record["status"] not in {"intake_committed", "intake_pending", "outcome_unknown"}
        ):
            raise OSError("unsafe prompt completion record")
        print("session-kit: prompt handoff has a terminal record and will not be replayed", file=sys.stderr)
        raise SystemExit(70)

    def record_completion(status, exit_code):
        atomic_record(completion, {
            "exit_code": exit_code,
            "managed_generation": managed_generation,
            "schema_version": 3,
            "sha256": digest,
            "status": status,
        })

    def move_handoff(destination):
        if destination.exists() or destination.is_symlink():
            raise OSError("conflicting prompt quarantine")
        path_info = os.stat(handoff.name, dir_fd=parent, follow_symlinks=False)
        if (path_info.st_dev, path_info.st_ino) != (info.st_dev, info.st_ino):
            raise OSError("prompt handoff pathname changed")
        os.rename(handoff.name, destination.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)

    accepted = exact_acceptance()
    if (acceptance.exists() or acceptance.is_symlink()) and not accepted:
        raise OSError("conflicting prompt acceptance record")
    if exact_intake(accepted):
        consume()
        raise SystemExit(0)
    if intake.exists() or intake.is_symlink():
        raise OSError("conflicting prompt intake record")
    refuse_prior_terminal()
    if accepted:
        move_handoff(intake_pending)
        record_completion("intake_pending", -1)
        print("session-kit: a prior Codex process accepted the prompt but durable intake is pending; moved to Needs You and will not replay", file=sys.stderr)
        raise SystemExit(70)

    environment = os.environ.copy()
    environment.update({
        "SESSION_KIT_PROMPT_HANDOFF": str(handoff),
        "SESSION_KIT_PROMPT_HANDOFF_ACCEPTANCE": str(acceptance),
        "SESSION_KIT_SOURCE_ACCEPTANCE_PATH": f"{handoff}.source_accepted",
        "SESSION_KIT_INTAKE_COMMIT_PATH": str(intake),
        "SESSION_KIT_PROMPT_HANDOFF_BYTES": str(len(payload)),
        "SESSION_KIT_PROMPT_HANDOFF_SHA256": digest,
        "SESSION_KIT_START_DIR": str(handoff.parent),
    })
    os.lseek(descriptor, 0, os.SEEK_SET)
    try:
        process = subprocess.Popen(command, stdin=descriptor, env=environment)
    except OSError:
        raise SystemExit(1)
    committed = False
    while process.poll() is None:
        accepted = exact_acceptance()
        if exact_intake(accepted):
            consume()
            committed = True
            break
        if (acceptance.exists() or acceptance.is_symlink()) and not accepted:
            break
        if intake.exists() or intake.is_symlink():
            break
        time.sleep(0.02)
    status = process.wait()
    accepted = exact_acceptance()
    if not committed and exact_intake(accepted):
        consume()
        committed = True
    if committed:
        record_completion("intake_committed", status)
        if status != 0:
            raise SystemExit(status if 0 < status < 256 else 1)
    elif accepted:
        move_handoff(intake_pending)
        if failpoint_armed("prompt-after-quarantine-move"):
            os._exit(91)
        record_completion("intake_pending", status)
        print("session-kit: Codex accepted the prompt but durable intake is pending; moved to Needs You and will not replay", file=sys.stderr)
        raise SystemExit(70)
    else:
        move_handoff(quarantine)
        if failpoint_armed("prompt-after-quarantine-move"):
            os._exit(91)
        record_completion("outcome_unknown", status)
        print("session-kit: Codex started without a durable intake result; moved to Needs You and will not replay", file=sys.stderr)
        raise SystemExit(status if 0 < status < 256 else 70)
except (OSError, UnicodeError):
    raise SystemExit(1)
finally:
    os.close(descriptor)
    os.close(parent)
PY
}

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
  __sk_launch="$__sk_start.launch"
  __sk_account="$__sk_start.account"
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
        IFS=$'\t' read -r __sk_launch_provider __sk_launch_cwd \
          __sk_requested_model __sk_launch_key __sk_launch_boot \
          __sk_launch_started __sk_launch_shell_pid __sk_launch_shell_start \
          __sk_launch_daemon_pid __sk_launch_daemon_start < "$__sk_launch"
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
          __sk_generation_ok=0
        fi
        unset __sk_account_json
      fi
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
        __sk_lifecycle_uuid=
        __sk_lifecycle_intake=
        __sk_lifecycle_generation=
        __sk_model_args=()
        if [[ -n $__sk_requested_model ]]; then
          __sk_model_args=(--model "$__sk_requested_model")
          export SESSION_KIT_REQUESTED_MODEL=$__sk_requested_model
          export SESSION_KIT_LAUNCH_IDEMPOTENCY_KEY=$__sk_launch_key
        else
          unset SESSION_KIT_REQUESTED_MODEL SESSION_KIT_LAUNCH_IDEMPOTENCY_KEY
        fi
        case "$__sk_provider" in
        claude)
          if ! command -v claude >/dev/null 2>&1; then
            echo "[session-kit: Claude is unavailable; launch record retained for retry]" >&2
          elif [[ $__sk_launch_mode != new ]] || \
              __sk_new_claude_uuid=$(python3 -c 'import uuid; print(uuid.uuid4())'); then
            __sk_consumed_records=("$__sk_start" "$__sk_expected")
            [[ -z $__sk_requested_model ]] || __sk_consumed_records+=("$__sk_launch")
            [[ ! -e $__sk_account ]] || __sk_consumed_records+=("$__sk_account")
            command rm -- "${__sk_consumed_records[@]}"
            unset __sk_consumed_records
            __sk_provider_launched=1
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
                  : > "$__sk_state_root/session-kit/provider-untitled/$SHPOOL_SESSION_NAME"
              ) 2>/dev/null || true
            fi
            unset __sk_claude_intent_uuid
            # Fleet Supervisor sessions attach their private MCP toolset at
            # launch. A resume proves itself against the published identity
            # marker; a first launch consumes the one-shot request that
            # bin/supervisor writes immediately before creating the session
            # (cwd-scoped, 2-minute freshness, consumed exactly once — a
            # mis-consume self-heals because refresh relaunches via resume).
            __sk_mcp_args=()
            __sk_sup_dir="$__sk_state_root/session-kit/supervisor"
            # --mcp-config launches commands at session start, so both marker
            # files get the pin reader's rigor: regular file, owned by this
            # user, mode 0600 exactly — anything else is inert.
            __sk_sup_private() {
              [[ -f $1 && ! -L $1 ]] || return 1
              [[ -n $(command find "$1" -maxdepth 0 -type f \
                        -user "$(id -un)" -perm 0600 2>/dev/null) ]]
            }
            if __sk_sup_private "$__sk_sup_dir/mcp.json"; then
              # An established supervisor proves itself by identity marker.
              if [[ $__sk_launch_mode != new ]] &&
                 __sk_sup_private "$__sk_sup_dir/identity"; then
                __sk_sup_id=$(command head -c 64 -- "$__sk_sup_dir/identity" 2>/dev/null)
                __sk_sup_id=${__sk_sup_id#claude:}
                __sk_sup_id=${__sk_sup_id//[$'\r\n']/}
                [[ $__sk_sup_id == "$__sk_uuid" ]] &&
                  __sk_mcp_args=(--mcp-config "$__sk_sup_dir/mcp.json")
                unset __sk_sup_id
              fi
              # A FIRST launch cannot match the marker (it is published after
              # creation) and, under name pre-baking, arrives as resume — not
              # new. So the one-shot request must be able to arm ANY mode the
              # marker could not vouch for: consume-once, cwd-scoped, 2-minute
              # fresh, exactly as before.
              if [[ ${#__sk_mcp_args[@]} -eq 0 ]] &&
                 __sk_sup_private "$__sk_sup_dir/launch-request"; then
                __sk_sup_req_cwd=$(command head -c 512 -- "$__sk_sup_dir/launch-request" 2>/dev/null)
                __sk_sup_req_cwd=${__sk_sup_req_cwd//[$'\r\n']/}
                __sk_sup_req_fresh=$(command find "$__sk_sup_dir/launch-request" -mmin -2 2>/dev/null)
                command rm -f -- "$__sk_sup_dir/launch-request"
                if [[ -n $__sk_sup_req_fresh && $__sk_sup_req_cwd == "$__sk_cwd" ]]; then
                  __sk_mcp_args=(--mcp-config "$__sk_sup_dir/mcp.json")
                fi
                unset __sk_sup_req_cwd __sk_sup_req_fresh
              fi
            fi
            while :; do
              case "$__sk_launch_mode" in
                new) claude "${__sk_model_args[@]}" --session-id "$__sk_new_claude_uuid" "${__sk_mcp_args[@]}"; __sk_provider_rc=$? ;;
                resume) claude "${__sk_model_args[@]}" --resume "$__sk_uuid" "${__sk_mcp_args[@]}"; __sk_provider_rc=$? ;;
                fork) claude "${__sk_model_args[@]}" --resume "$__sk_uuid" --fork-session "${__sk_mcp_args[@]}"; __sk_provider_rc=$? ;;
              esac
              # A kit-requested bounce relaunches the SAME conversation once,
              # so the fresh process boots through SessionStart with its name
              # intent applied. Any other exit falls through to the normal
              # provider-exit handling.
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
          else
            echo "[session-kit: Claude session identity could not be allocated; launch record retained for retry]" >&2
          fi
          unset __sk_new_claude_uuid __sk_mcp_args __sk_sup_dir
          unset -f __sk_sup_private 2>/dev/null
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
                  __sk_prompt_handoff="$__sk_start_dir/$SHPOOL_SESSION_NAME.prompt"
                  __sk_prompt_acceptance="$__sk_prompt_handoff.accepted"
                  __sk_prompt_intake="$__sk_prompt_handoff.intake_committed"
                  __sk_prompt_completion="$__sk_prompt_handoff.completed"
                  if [[ -f $__sk_prompt_handoff && ! -L $__sk_prompt_handoff ]]; then
                    __sk_lifecycle_generation="$__sk_current_boot_id:$__sk_shell_pid:$__sk_shell_start:$__sk_started"
                    __sk_lifecycle_intake=$__sk_prompt_intake
                    SESSION_KIT_MANAGED_GENERATION="$__sk_lifecycle_generation" \
                    __sk_codex_exec_handoff "$__sk_prompt_handoff" \
                      "$__sk_prompt_acceptance" "$__sk_prompt_intake" \
                      "$__sk_prompt_completion" codex \
                      "${__sk_codex_no_update[@]}" "${__sk_codex_theme[@]}" \
                      "${__sk_model_args[@]}" exec -
                    __sk_provider_rc=$?
                    __sk_lifecycle_uuid=$(python3 - "$__sk_prompt_acceptance" <<'PY' 2>/dev/null || true
import json
import re
import sys
try:
    value = json.load(open(sys.argv[1], encoding="utf-8"))
    session_id = value.get("session_id", "")
except (OSError, ValueError):
    session_id = ""
if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", session_id):
    print(session_id)
PY
                    )
                  else
                    codex "${__sk_codex_no_update[@]}" "${__sk_codex_theme[@]}" \
                      "${__sk_model_args[@]}" "${__sk_codex_remote[@]}" --no-alt-screen
                    __sk_provider_rc=$?
                  fi
                  unset __sk_prompt_handoff __sk_prompt_acceptance __sk_prompt_intake __sk_prompt_completion
                  ;;
                resume) codex "${__sk_codex_no_update[@]}" "${__sk_codex_theme[@]}" "${__sk_model_args[@]}" "${__sk_codex_remote[@]}" --no-alt-screen resume "$__sk_uuid"; __sk_provider_rc=$? ;;
                fork) codex "${__sk_codex_no_update[@]}" "${__sk_codex_theme[@]}" "${__sk_model_args[@]}" "${__sk_codex_remote[@]}" --no-alt-screen fork "$__sk_uuid"; __sk_provider_rc=$? ;;
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
             SESSION_KIT_LIFECYCLE_CONVERSATION_UUID=$__sk_lifecycle_uuid \
             SESSION_KIT_LIFECYCLE_INTAKE_COMMIT=$__sk_lifecycle_intake \
             SESSION_KIT_MANAGED_GENERATION=$__sk_lifecycle_generation \
             SESSION_KIT_REQUESTED_MODEL=$__sk_requested_model \
             SESSION_KIT_LAUNCH_IDEMPOTENCY_KEY=$__sk_launch_key \
             python3 "$__sk_lifecycle_core" lifecycle provider-exited >/dev/null
          then
            __sk_provider_exited=1
            __sk_provider_exit_code=$__sk_provider_rc
          else
            echo "[session-kit: provider exited; lifecycle proof is unavailable, so automatic cleanup is blocked]" >&2
          fi
          # An account switch is requested from a different Session Kit
          # window.  The exact provider has exited and any Codex App Server
          # children have already been reaped, so it is now safe to move the
          # exact transcript and re-enter this same managed shell generation.
          __sk_switch_request="$__sk_state_root/session-kit/account-switch-requests/$SHPOOL_SESSION_NAME"
          if [[ -e $__sk_switch_request || -L $__sk_switch_request ]]; then
            __sk_switch_ok=0
            __sk_switch_action= __sk_switch_txid= __sk_switch_alias=
            __sk_switch_fallback= __sk_switch_uuid=
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
    or payload[:-1].count(b"\t") != 4
    or b"\r" in payload
    or b"\x1c" in payload
):
    raise SystemExit(1)
PY
            then
              IFS=$'\t' read -r __sk_switch_action __sk_switch_txid \
                __sk_switch_alias __sk_switch_fallback __sk_switch_uuid \
                < "$__sk_switch_request"
              if [[ $__sk_switch_action =~ ^(apply|rollback)$ &&
                    $__sk_switch_txid =~ ^[0-9a-f]{32}$ &&
                    $__sk_switch_alias =~ ^[a-z][a-z0-9_-]{0,11}$ &&
                    $__sk_switch_fallback =~ ^[a-z][a-z0-9_-]{0,11}$ &&
                    $__sk_switch_uuid == "$__sk_lifecycle_uuid" ]]; then
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
                    chmod 600 -- "$__sk_switch_tmp" "$__sk_switch_expected_tmp" \
                      "$__sk_switch_account_tmp" &&
                    mv -- "$__sk_switch_tmp" "$__sk_start" &&
                    mv -- "$__sk_switch_expected_tmp" "$__sk_expected" &&
                    mv -- "$__sk_switch_account_tmp" "$__sk_account"
                  ) && __sk_switch_ok=1
                  command rm -f -- "$__sk_switch_tmp" "$__sk_switch_expected_tmp" \
                    "$__sk_switch_account_tmp"
                fi
              fi
            fi
            if (( __sk_switch_ok )); then
              command rm -f -- "$__sk_switch_request"
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
  unset __sk_start_line __sk_expected_line __sk_record_shape_ok
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
  unset __sk_account_ok __sk_account_provider __sk_account_alias __sk_account_profile
  unset SESSION_KIT_REQUESTED_MODEL SESSION_KIT_LAUNCH_IDEMPOTENCY_KEY

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

  # Any provider exit — clean or crashed — stops at the recovery menu and
  # leaves the terminal open. A clean /exit is still a decision, but closing
  # the window on it throws away the one thing that makes the exact
  # conversation recoverable: a live terminal that still knows its identity,
  # with r to reopen, k to keep, and s to drop to a shell. Reaching the picker
  # is one more keypress (c); losing the terminal is not undoable. Touch
  # ~/.sk_autoclose_on_clean_exit to opt this machine back into closing the
  # terminal on a zero exit code; a non-zero exit always stops at the menu.
  if [[ ${__sk_provider_exited:-0} == 1 ]]; then
    if (( ${__sk_provider_exit_code:-1} == 0 )) && [[ -e $HOME/.sk_autoclose_on_clean_exit ]]; then
      __sk_lifecycle_event user-input >/dev/null 2>&1 || true
      builtin exit
    fi
    printf '\n%s exited with status %s. This terminal is still open.\n' \
      "${__sk_lifecycle_provider^}" "${__sk_provider_exit_code:-unknown}"
    __sk_provider_exit_menu
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
  PS1="\[\e[38;5;71m\]▌\[\e[0m\] \$(__sk_waiting)$PS1"
fi

unset __sk_state_root __sk_journal_root __sk_session_journal __sk_journal_ready __sk_script_rc

# `kit` opens the session picker on demand (maintainer decision, 2026-08-02:
# SSH lands in a regular shell; the picker only ever appears when typed).
# Works from any plain terminal; inside a managed session it shows the read-only list
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
