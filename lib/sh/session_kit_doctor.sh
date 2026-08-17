#!/usr/bin/env bash
# Session Kit doctor command: the read-only installation report.
# Source this file; do not execute it.
#
# Source order: bin/session-kit sources this module after it assigns the
# lifecycle roots, helpers, common_required_commands, launchd_units,
# launchd_template_root, transaction_path, and integration_marker, and after it
# defines die() and platform(). doctor_command() also calls
# current_release_id(), verify_release(), stat_mode(),
# systemd_user_manager_transport(), and validate_purge_ownership() from the
# checks and lifecycle modules.
#
# Globals the entry script owns are assigned there, not here.
# shellcheck disable=SC2154

# Run one read-only systemctl user query over whichever transport the manager
# actually answers on. The caller resolves the transport once so a report never
# probes for it per unit.
doctor_systemctl_user() {
  local transport=$1 tool=${SESSION_KIT_SYSTEMCTL_CMD:-systemctl}
  shift
  if [[ $transport == machine ]]; then
    "$tool" --user --machine=@.host "$@"
  else
    "$tool" --user "$@"
  fi
}

# Which release a live process is actually executing. Bash keeps the script it
# exec'd open on descriptor 255, and that descriptor resolves through the
# `current` symlink to the release directory as it was at exec time -- which is
# exactly the question `current` itself can no longer answer. The exported
# release directory is the fallback for a process whose descriptors are not
# readable.
doctor_running_release() {
  local pid=$1 path=
  [[ $pid =~ ^[0-9]+$ && $pid -gt 0 ]] || return 1
  path=$(readlink "/proc/$pid/fd/255" 2>/dev/null) || path=
  if [[ $path != */releases/* ]]; then
    path=$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null |
      sed -n 's|^SESSION_KIT_RELEASE_DIR=||p' | head -n 1) || path=
  fi
  [[ $path == */releases/* ]] || return 1
  path=${path#*/releases/}
  path=${path%%/*}
  [[ $path =~ ^[0-9a-f]{40}$ ]] || return 1
  printf '%s\n' "$path"
}

# Report what systemd says about the Session Kit units, not what the file
# system says. `session-kit services enable` installs unit files for every
# unit, so a file-exists check passes on a machine where the watchdog has
# never run and broken sessions are never restored.
#
# Called from doctor_command(), which owns add_check(), the checks array, and
# release_id.
# shellcheck disable=SC2154
doctor_linux_service_truth() {
  local transport=$1 unit=  probe=1 show= key= value=
  local file_state= active_state= detail= summary= degraded=0
  local watchdog=session-kit-watchdog.service
  local fix="session-kit services enable"
  # Live state is only read when the user manager answers on its own socket.
  # A manager reachable only through the local-machine transport is already
  # degraded, and logind usually has no session record for the user in that
  # state, so both readings would be guesses. Test installs point $service_root
  # at a fixture directory the real manager has never read, so probing there
  # would report this developer machine instead of the fixture.
  if [[ -n ${SESSION_KIT_SYSTEMCTL_CMD:-} ]]; then
    probe=1
  elif [[ ${SESSION_KIT_TESTING:-0} == 1 || $transport != direct ]]; then
    probe=0
  fi
  read_unit_state() {
    file_state=
    active_state=
    show=$(doctor_systemctl_user "$transport" show \
      --property=UnitFileState --property=ActiveState -- "$1" 2>/dev/null) ||
      show=
    [[ -n $show ]] || return 1
    while IFS='=' read -r key value; do
      case $key in
        UnitFileState) file_state=$value ;;
        ActiveState) active_state=$value ;;
      esac
    done <<<"$show"
    return 0
  }
  if [[ ! -f $service_root/$watchdog || -L $service_root/$watchdog ]]; then
    add_check fail watchdog \
      "unit file is missing from $service_root; reinstall the units with: session-kit update"
  elif (( probe == 0 )); then
    add_check warn watchdog \
      "unit file is installed; the live unit state was not probed here"
  elif ! read_unit_state "$watchdog"; then
    add_check warn watchdog \
      "unit file is installed; systemd did not answer a state query, so it could not be confirmed running"
  elif [[ $file_state != enabled ]]; then
    add_check fail watchdog \
      "installed but not enabled (${file_state:-unknown}), so broken sessions are never restored; enable it with: $fix"
  elif [[ $active_state != active ]]; then
    add_check fail watchdog \
      "enabled but not running ($active_state), so broken sessions are never restored; start it with: $fix"
  else
    add_check ok watchdog "enabled and running"
  fi
  # The socket and the timers carry the daemon, the reaper, and the sub-agent
  # sweep. The units they activate are reported for context and never required
  # to be running. The sweep timer is checked like the others because the
  # fifteen-minute close rule is only true while it runs: a timer that quietly
  # never got enabled turns that rule back into the hourly one.
  for unit in shpool.socket shpool-reaper.timer session-kit-subagent-sweep.timer \
    shpool.service shpool-reaper.service session-kit-subagent-sweep.service; do
    if [[ ! -f $service_root/$unit || -L $service_root/$unit ]]; then
      summary+="${summary:+; }$unit missing from $service_root"
      degraded=2
      continue
    fi
    if (( probe == 0 )) || ! read_unit_state "$unit"; then
      summary+="${summary:+; }$unit installed, state unread"
      (( degraded >= 1 )) || degraded=1
      continue
    fi
    detail="$unit ${file_state:-unknown}/${active_state:-unknown}"
    case $unit in
      shpool.service) detail+=" (socket-activated)" ;;
      shpool-reaper.service) detail+=" (timer-activated)" ;;
      session-kit-subagent-sweep.service) detail+=" (timer-activated)" ;;
      *)
        if [[ $file_state != enabled || $active_state != active ]]; then
          degraded=2
        fi
        ;;
    esac
    summary+="${summary:+; }$detail"
  done
  case $degraded in
    0) add_check ok units "$summary" ;;
    1) add_check warn units "$summary" ;;
    *) add_check fail units "$summary; enable the units with: $fix" ;;
  esac
  # Is what is running what we shipped? Everything above reports on FILES: the
  # unit exists, it is enabled, something is active. None of that can see a
  # process still executing the release it started with, or a systemd that has
  # not read the unit files this install wrote. Ten activations passed over one
  # watchdog process while every check above stayed green, so the fix it shipped
  # ran nowhere. These two checks are the question those cannot ask.
  local shown= reload= main_pid= running=
  if (( probe == 0 )); then
    add_check warn release-running \
      "the running units were not compared with the installed release here"
  else
    shown=$(doctor_systemctl_user "$transport" show \
      --property=NeedDaemonReload --property=MainPID -- "$watchdog" 2>/dev/null) ||
      shown=
    reload=
    main_pid=
    while IFS='=' read -r key value; do
      case $key in
        NeedDaemonReload) reload=$value ;;
        MainPID) main_pid=$value ;;
      esac
    done <<<"$shown"
    if [[ $reload == yes ]]; then
      add_check fail units-loaded \
        "systemd is serving unit definitions this release replaced; load them with: systemctl --user daemon-reload"
    elif [[ -z $reload ]]; then
      add_check warn units-loaded \
        "systemd did not answer whether the unit files on disk have been loaded"
    else
      add_check ok units-loaded "systemd holds the installed unit definitions"
    fi
    if [[ ! $main_pid =~ ^[0-9]+$ ]] || (( main_pid == 0 )); then
      add_check warn release-running \
        "the watchdog is not running, so there is nothing to compare with the installed release"
    elif ! running=$(doctor_running_release "$main_pid"); then
      add_check warn release-running \
        "the running watchdog's release could not be read from the process table"
    elif [[ $running != "$release_id" ]]; then
      add_check fail release-running \
        "the watchdog is running release ${running:0:12}, not the installed ${release_id:0:12}, so every fix since then is inert; restart it with: systemctl --user restart $watchdog"
    else
      add_check ok release-running "the watchdog is running the installed release"
    fi
  fi
  local linger_status= linger_detail=
  if (( probe == 0 )); then
    add_check warn linger \
      "lingering was not checked here; without it logind stops the user manager at logout and every managed session ends with it"
  else
    IFS=$'\t' read -r linger_status linger_detail < <(logind_linger_report)
    add_check "$linger_status" linger "$linger_detail"
  fi
}

# Can every session this machine recorded still be read by the tools that read
# transcripts? The measured defect this answers: a live session running under a
# rotated account profile had its transcript on this disk and the tool asking
# for it reported none, because one resolver knew a root the other did not.
# Nothing warned; the blindness was visible only to whoever happened to ask
# that session for its history.
#
# The resolver is the kit's own, so this check goes blind only if the resolver
# does -- and then it says so. Read-only: the recorded list is read, never
# built.
#
# Prints one "status<TAB>detail" line. Called from doctor_command().
doctor_transcript_report() {
  local core=$install_root/current/lib/session_inventory.py
  if [[ ! -f $core || -L $core ]]; then
    printf 'warn\t%s\n' \
      "transcript reachability was not checked: the release library is missing at $core"
    return 0
  fi
  local report=
  report=$(SESSION_KIT_STATE_DIR=$state_root python3 "$core" transcript reachability 2>/dev/null) || {
    printf 'warn\ttranscript reachability could not be checked\n'
    return 0
  }
  printf '%s' "$report" | python3 -c '
import json
import sys

try:
    value = json.load(sys.stdin)
    status = value["status"]
    detail = " ".join(str(value["detail"]).replace("\t", " ").split())[:500]
except (ValueError, KeyError, TypeError):
    print("warn\ttranscript reachability could not be read")
else:
    print(f"{status}\t{detail}")
' || printf 'warn\ttranscript reachability could not be read\n'
}

doctor_command() {
  local json=0
  [[ ${1:-} != --json ]] || { json=1; shift; }
  (($# == 0)) || die "unknown doctor option: $1"
  local current_platform release_id= failed=0 helper required row status name detail
  current_platform=$(platform)
  release_id=$(current_release_id)
  local -a checks=()
  add_check() {
    checks+=("$1"$'\t'"$2"$'\t'"$3")
    [[ $1 != fail ]] || failed=1
  }
  if [[ $current_platform != unsupported ]]; then
    add_check ok platform "$current_platform"
  else
    add_check fail platform "unsupported operating system; Session Kit needs Linux or macOS"
  fi
  if [[ $release_id =~ ^[0-9a-f]{40}$ &&
        -d $install_root/releases/$release_id ]]; then
    local release_error=
    if release_error=$(verify_release "$install_root/releases/$release_id" "$release_id" 2>&1); then
      add_check ok release "$release_id"
    else
      add_check fail release "${release_error#session-kit: }"
    fi
  else
    add_check fail release "no valid active release"
  fi
  local manager_id= manager_dir= manager_error=
  if [[ -L $manager_link ]] &&
     manager_dir=$(cd -P -- "$manager_link" 2>/dev/null && pwd) &&
     manager_id=${manager_dir##*/} &&
     [[ $manager_id =~ ^[0-9a-f]{40}$ ]] &&
     [[ $manager_dir == "$install_root/releases/$manager_id" ]] &&
     manager_error=$(verify_release "$manager_dir" "$manager_id" 2>&1); then
    add_check ok manager "$manager_id"
    if [[ -f $bin_dir/session-kit && ! -L $bin_dir/session-kit &&
          -f $manager_dir/deploy/session-kit-launcher &&
          ! -L $manager_dir/deploy/session-kit-launcher ]] &&
       cmp -s -- "$bin_dir/session-kit" "$manager_dir/deploy/session-kit-launcher"; then
      add_check ok manager-launcher "stable launcher matches the management release"
    else
      add_check fail manager-launcher "stable launcher does not match the management release"
    fi
  else
    add_check fail manager "management release anchor is missing, unsafe, or invalid${manager_error:+: ${manager_error#session-kit: }}"
    add_check fail manager-launcher "management release could not be checked"
  fi
  local -a required_commands=("${common_required_commands[@]}") missing_commands=()
  if [[ $current_platform == linux ]]; then
    required_commands+=(flock journalctl sha256sum systemctl)
  elif [[ $current_platform == macos ]]; then
    required_commands+=(launchctl plutil sw_vers)
  fi
  for required in "${required_commands[@]}"; do
    command -v "$required" >/dev/null 2>&1 || missing_commands+=("$required")
  done
  if ((${#missing_commands[@]} == 0)); then
    add_check ok prerequisites "all required commands are available"
  else
    add_check fail prerequisites "commands unavailable: ${missing_commands[*]}"
  fi
  local shpool_status= shpool_detail=
  IFS=$'\t' read -r shpool_status shpool_detail < <(shpool_version_report)
  add_check "$shpool_status" shpool-version "$shpool_detail"
  if [[ $current_platform == linux ]]; then
    if [[ -d /proc || ${SESSION_KIT_TESTING:-0} == 1 ]]; then
      add_check ok process "Linux /proc is available"
    else
      add_check fail process "/proc is unavailable"
    fi
    local manager_transport=
    if [[ ${SESSION_KIT_TESTING:-0} == 1 ]]; then
      add_check ok services "systemd user manager is available"
    elif manager_transport=$(systemd_user_manager_transport); then
      if [[ $manager_transport == direct ]]; then
        add_check ok services "systemd user manager is available"
      else
        add_check warn services "systemd user manager is available through local-machine transport; direct user socket is unavailable"
      fi
    else
      add_check fail services "systemd user manager is unavailable"
    fi
    doctor_linux_service_truth "$manager_transport"
  elif [[ $current_platform == macos ]]; then
    if [[ ${SESSION_KIT_TESTING:-0} == 1 ]] ||
        python3 "$install_root/current/lib/session_inventory.py" platform boot-id >/dev/null 2>&1; then
      add_check ok process "Darwin native process adapter is available"
    else
      add_check fail process "Darwin native process adapter is unavailable"
    fi
    local loaded=0 unit label
    for unit in "${launchd_units[@]}"; do
      [[ -f $launchd_template_root/$unit && ! -L $launchd_template_root/$unit ]] || {
        add_check fail services "missing LaunchAgent: $unit"
        continue
      }
      plutil -lint "$launchd_template_root/$unit" >/dev/null 2>&1 || {
        add_check fail services "invalid LaunchAgent: $unit"
        continue
      }
      label=${unit%.plist}
      launchctl print "gui/$UID/$label" >/dev/null 2>&1 && loaded=$((loaded + 1))
    done
    if (( loaded == ${#launchd_units[@]} )); then
      add_check ok services "all Session Kit LaunchAgents are loaded"
    elif (( loaded == 0 )); then
      add_check warn services "LaunchAgents are installed but unloaded"
    else
      add_check warn services "$loaded of ${#launchd_units[@]} LaunchAgents are loaded"
    fi
  fi
  if command -v claude >/dev/null 2>&1 || command -v codex >/dev/null 2>&1; then
    add_check ok provider "Claude Code or Codex is available"
  else
    add_check fail provider "no provider is installed; install Claude Code, Codex, or both"
  fi
  local transcript_status= transcript_detail= transcript_line=
  # Tab is IFS whitespace: an empty first field would collapse and put the
  # detail in the status. Translate to \034 before splitting (the husk rule).
  IFS= read -r transcript_line < <(doctor_transcript_report)
  transcript_line=${transcript_line//$'\t'/$'\034'}
  IFS=$'\034' read -r transcript_status transcript_detail <<<"$transcript_line"
  add_check "${transcript_status:-warn}" transcripts \
    "${transcript_detail:-transcript reachability could not be checked}"
  local audit_output audit_ok=1 audit_name
  local -a audit_names=(
    claude-version codex-version codex-themes naming-instructions naming-hook
    statusline claude-integration-ledger claude-statusline-backups
    tab-title codex-titles attention-hook internal-formats
    kill-switches subagent-sweep acceptance shpool-binary
  )
  declare -A audit_expected=() audit_seen=()
  for audit_name in "${audit_names[@]}"; do audit_expected[$audit_name]=1; done
  if audit_output=$(python3 - "$HOME" "$config_root" \
    "${SESSION_KIT_CODEX_HOME:-${CODEX_HOME:-$HOME/.codex}}" \
    "$release_id" "$current_platform" "$install_root" <<'PY'
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

home, config_root, codex_home = map(Path, sys.argv[1:4])
active_release, active_platform = sys.argv[4:6]
install_root = Path(sys.argv[6])


def emit(status: str, name: str, detail: str) -> None:
    safe = " ".join(str(detail).replace("\t", " ").split())[:500]
    print(status, name, safe, sep="\t")


def terminate_group(child: subprocess.Popen[bytes]) -> bool:
    cleanup_failed = False
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        cleanup_failed = True
    try:
        child.wait(timeout=0.25)
    except subprocess.TimeoutExpired:
        pass
    except OSError:
        cleanup_failed = True
    time.sleep(0.05)
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        cleanup_failed = True
    if child.poll() is None:
        try:
            child.wait(timeout=0.5)
        except (subprocess.TimeoutExpired, OSError):
            cleanup_failed = True
    return not cleanup_failed


def provider_version(command: str) -> tuple[str, str]:
    executable = shutil.which(command)
    if not executable:
        return "ok", f"{command} is not installed"
    try:
        child = subprocess.Popen(
            [executable, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError:
        return "warn", f"{command} version could not be read"
    payload = bytearray()
    deadline = time.monotonic() + 3.0
    overflow = timed_out = read_failed = False
    cleanup_ok = True
    try:
        assert child.stdout is not None
        descriptor = child.stdout.fileno()
        while time.monotonic() < deadline:
            ready, _, _ = select.select([descriptor], [], [], 0.1)
            if ready:
                block = os.read(descriptor, 257 - len(payload))
                if not block:
                    break
                payload.extend(block)
                if len(payload) > 256:
                    overflow = True
                    break
            elif child.poll() is not None:
                break
        else:
            timed_out = True
    except (OSError, ValueError):
        read_failed = True
    finally:
        cleanup_ok = terminate_group(child)
        if child.stdout is not None:
            child.stdout.close()
    if not cleanup_ok:
        return "warn", f"{command} version process could not be reaped"
    if read_failed:
        return "warn", f"{command} version could not be read"
    if timed_out:
        return "warn", f"{command} --version exceeded 3 seconds"
    if overflow:
        return "warn", f"{command} --version exceeded 256 bytes"
    try:
        text = " ".join(payload.decode("utf-8", "strict").split())
    except UnicodeError:
        return "warn", f"{command} reported an unrecognized version"
    if child.returncode != 0 or not text:
        return "warn", f"{command} version was unavailable"
    patterns = {
        "claude": r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+._A-Za-z0-9]*)? \(Claude Code\)$",
        "codex": r"^codex-cli [0-9]+\.[0-9]+\.[0-9]+(?:[-+._A-Za-z0-9]*)?$",
    }
    if re.fullmatch(patterns[command], text) is None:
        return "warn", f"{command} reported an unrecognized version"
    return "ok", text


provider_observed = {}
provider_ready = {}
for provider in ("claude", "codex"):
    installed = shutil.which(provider) is not None
    status, detail = provider_version(provider)
    emit(status, f"{provider}-version", detail)
    provider_ready[provider] = not installed or status == "ok"
    provider_observed[provider] = detail if installed and status == "ok" else None

theme_names = (
    "red", "blue", "green", "yellow", "purple", "orange", "pink", "cyan",
    "lime", "magenta", "silver", "sand", "sky", "sea",
)
def secure_regular_bytes(
    path: Path,
    limit: int,
    *,
    exact_mode: int | None = None,
    executable: bool = False,
) -> bytes | None:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        before = os.fstat(descriptor)
        mode = stat.S_IMODE(before.st_mode)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or before.st_size > limit
            or (exact_mode is not None and mode != exact_mode)
            or (executable and mode & 0o111 == 0)
        ):
            return None
        current = os.lstat(path)
        if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            return None
        payload = bytearray()
        while len(payload) <= limit:
            block = os.read(descriptor, min(65_536, limit + 1 - len(payload)))
            if not block:
                break
            payload.extend(block)
        after = os.fstat(descriptor)
        if (
            len(payload) > limit
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        ):
            return None
        return bytes(payload)
    except OSError:
        return None
    finally:
        os.close(descriptor)


def bounded_text(path: Path, limit: int = 1_048_576) -> str | None:
    payload = secure_regular_bytes(path, limit)
    if payload is None:
        return None
    try:
        return payload.decode("utf-8", "strict")
    except UnicodeError:
        return None


def safe_directory_chain(path: Path, *, final_owner: bool = False) -> bool:
    if not path.is_absolute() or ".." in path.parts:
        return False
    try:
        relative = path.relative_to(home)
    except ValueError:
        current = Path(path.anchor)
        relative = path.relative_to(current)
    else:
        current = home
    components = [current]
    for part in relative.parts:
        current = current / part
        components.append(current)
    try:
        for index, component in enumerate(components):
            info = component.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or info.st_uid not in {0, os.geteuid()}
                or stat.S_IMODE(info.st_mode) & 0o022
                or (index == len(components) - 1 and final_owner and info.st_uid != os.geteuid())
            ):
                return False
        return True
    except OSError:
        return False


# The watchdog compares the running shpool daemon against a fingerprint
# recorded in private state. Two states leave that check present but no longer
# protecting anything, and both are silent: an absent fingerprint skips the
# check entirely, and a stale one reports a change on every pass until the
# report stops being read. Report each distinctly rather than as one failure.
state_root = Path(os.environ.get("XDG_STATE_HOME") or (home / ".local/state"))
kit_state_root = Path(
    os.environ.get("SESSION_KIT_STATE_DIR") or (state_root / "session-kit")
)
fingerprint_bytes = secure_regular_bytes(
    state_root / "session-kit" / "shpool-binary.sha256", 4096
)
recorded_fingerprint = None
if fingerprint_bytes is not None:
    candidate = fingerprint_bytes.decode("utf-8", "replace").strip()
    if re.fullmatch(r"[0-9a-f]{64}", candidate):
        recorded_fingerprint = candidate
if fingerprint_bytes is None:
    emit(
        "warn",
        "shpool-binary",
        "no fingerprint recorded, so the watchdog binary check is inactive",
    )
elif recorded_fingerprint is None:
    emit(
        "warn",
        "shpool-binary",
        "recorded fingerprint is not a sha256 digest, so the check is inactive",
    )
else:
    shpool_path = shutil.which("shpool")
    if not shpool_path:
        emit("warn", "shpool-binary", "shpool is not on PATH, so it cannot be compared")
    else:
        installed_bytes = secure_regular_bytes(Path(shpool_path), 512 * 1_048_576)
        if installed_bytes is None:
            emit("warn", "shpool-binary", "installed shpool binary could not be read")
        elif hashlib.sha256(installed_bytes).hexdigest() == recorded_fingerprint:
            emit("ok", "shpool-binary", "recorded fingerprint matches the installed shpool")
        else:
            emit(
                "warn",
                "shpool-binary",
                "recorded fingerprint no longer matches "
                + shpool_path
                + "; re-record it after a deliberate rebuild",
            )

theme_errors = []
codex_root_safe = safe_directory_chain(codex_home, final_owner=True)
themes_root = codex_home / "themes"
themes_layout_safe = codex_root_safe and safe_directory_chain(
    themes_root, final_owner=True
)
for color in theme_names:
    path = themes_root / f"sk-{color}.tmTheme"
    if not themes_layout_safe or secure_regular_bytes(
        path, 1_048_576, exact_mode=0o600
    ) is None:
        theme_errors.append(color)
if theme_errors:
    emit("warn", "codex-themes", "missing or unsafe themes: " + ", ".join(theme_errors))
else:
    emit(
        "ok",
        "codex-themes",
        f"all {len(theme_names)} themes are private regular readable files",
    )

instruction_errors = []
for label, path in (
    ("Codex", codex_home / "AGENTS.md"),
    ("Claude", home / ".claude" / "CLAUDE.md"),
):
    text = None if label == "Codex" and not codex_root_safe else bounded_text(path)
    if text is None or "sp self-name" not in text:
        instruction_errors.append(label)
if instruction_errors:
    emit("warn", "naming-instructions", "self-name instruction missing for: " + ", ".join(instruction_errors))
else:
    emit("ok", "naming-instructions", "Codex and Claude self-name instructions are present")

hook_errors = []
statusline_errors = []
claude_root = Path(os.environ.get("SESSION_KIT_CLAUDE_HOME") or (home / ".claude"))
hook_path = claude_root / "hooks" / "nameintent_title.sh"
statusline_path = claude_root / "statusline.sh"
hook_payload = secure_regular_bytes(
    hook_path, 1_048_576, executable=True, exact_mode=0o700
)
if hook_payload is None:
    hook_errors.append("hook file")
statusline_payload = secure_regular_bytes(
    statusline_path, 1_048_576, executable=True, exact_mode=0o700
)
if statusline_payload is None:
    statusline_errors.append("status line file")
release_claude = install_root / "releases" / active_release / "config" / "claude"
ledger_path = kit_state_root / "claude-integration.json"
ledger_info = None
ledger_exists = False
ledger_reason = None
try:
    ledger_info = ledger_path.lstat()
    ledger_exists = True
except FileNotFoundError:
    pass
except OSError as exc:
    ledger_exists = True
    ledger_reason = f"could not be inspected: {exc.strerror or exc}"
ledger_bytes = secure_regular_bytes(ledger_path, 1_048_576)
ledger_files = None
if ledger_exists and ledger_bytes is None:
    if ledger_reason is None and ledger_info is not None:
        if stat.S_ISLNK(ledger_info.st_mode):
            ledger_reason = "is a symbolic link"
        elif not stat.S_ISREG(ledger_info.st_mode):
            ledger_reason = "is not a regular file"
        elif ledger_info.st_uid != os.geteuid():
            ledger_reason = "is not owned by the current user"
        elif ledger_info.st_nlink != 1:
            ledger_reason = "has multiple hard links"
        elif ledger_info.st_size > 1_048_576:
            ledger_reason = "is larger than 1 MiB"
        else:
            ledger_reason = "could not be read safely"
elif ledger_bytes is not None:
    try:
        ledger = json.loads(ledger_bytes.decode("utf-8", "strict"))
        files = ledger.get("files") if isinstance(ledger, dict) else None
        if (
            ledger.get("schema_version") != 1
            or not isinstance(ledger.get("release_id"), str)
            or re.fullmatch(r"[0-9a-f]{40}", ledger["release_id"]) is None
            or not isinstance(files, dict)
            or set(files) != {str(hook_path), str(statusline_path)}
            or any(
                not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                for digest in files.values()
            )
        ):
            raise ValueError
        ledger_files = files
    except (UnicodeError, ValueError, AttributeError):
        ledger_reason = "contains invalid data"

if ledger_files is not None:
    emit("ok", "claude-integration-ledger", "integration ledger is valid")
elif ledger_exists:
    emit(
        "warn",
        "claude-integration-ledger",
        "existing integration ledger " + (ledger_reason or "could not be used safely"),
    )
    hook_errors.append("integration ledger")
    statusline_errors.append("integration ledger")
else:
    emit(
        "ok",
        "claude-integration-ledger",
        "no integration ledger; using pre-ledger compatibility checks",
    )

backups_path = kit_state_root / "claude-statusline-backups.json"
backups_info = None
backups_exists = False
backups_reason = None
try:
    backups_info = backups_path.lstat()
    backups_exists = True
except FileNotFoundError:
    pass
except OSError as exc:
    backups_exists = True
    backups_reason = f"could not be inspected: {exc.strerror or exc}"
backups_bytes = secure_regular_bytes(backups_path, 1_048_576)
backups_valid = False
if backups_exists and backups_bytes is None:
    if backups_reason is None and backups_info is not None:
        if stat.S_ISLNK(backups_info.st_mode):
            backups_reason = "is a symbolic link"
        elif not stat.S_ISREG(backups_info.st_mode):
            backups_reason = "is not a regular file"
        elif backups_info.st_uid != os.geteuid():
            backups_reason = "is not owned by the current user"
        elif backups_info.st_nlink != 1:
            backups_reason = "has multiple hard links"
        elif backups_info.st_size > 1_048_576:
            backups_reason = "is larger than 1 MiB"
        else:
            backups_reason = "could not be read safely"
elif backups_bytes is not None:
    try:
        backups = json.loads(backups_bytes.decode("utf-8", "strict"))
        entries = backups.get("entries") if isinstance(backups, dict) else None
        if (
            not isinstance(backups, dict)
            or backups.get("schema_version") != 1
            or not isinstance(entries, dict)
            or any(
                not isinstance(key, str)
                or not Path(key).is_absolute()
                or not isinstance(record, dict)
                or not isinstance(record.get("present"), bool)
                or set(record) - {"present", "value"}
                or (record["present"] and "value" not in record)
                for key, record in entries.items()
            )
        ):
            raise ValueError
        backups_valid = True
    except (UnicodeError, ValueError, AttributeError):
        backups_reason = "contains invalid data"

if backups_valid:
    emit("ok", "claude-statusline-backups", "status-line backup ledger is valid")
elif backups_exists:
    emit(
        "warn",
        "claude-statusline-backups",
        "existing status-line backup ledger "
        + (backups_reason or "could not be used safely"),
    )
else:
    emit(
        "ok",
        "claude-statusline-backups",
        "no status-line backup ledger; nothing is pending for restoration",
    )

if ledger_files is not None:
    if hook_payload is not None and (
        hashlib.sha256(hook_payload).hexdigest() != ledger_files[str(hook_path)]
    ):
        hook_errors.append("hook content")
    if statusline_payload is not None and (
        hashlib.sha256(statusline_payload).hexdigest()
        != ledger_files[str(statusline_path)]
    ):
        statusline_errors.append("status line content")
elif not ledger_exists:
    # Compatibility with installations that predate the integration ledger.
    expected_hook = secure_regular_bytes(
        release_claude / "nameintent_title.sh", 1_048_576
    )
    expected_statusline = secure_regular_bytes(
        release_claude / "statusline.sh", 1_048_576
    )
    if hook_payload is not None and (
        expected_hook is None or hook_payload != expected_hook
    ):
        hook_errors.append("hook content")
    if statusline_payload is not None and (
        expected_statusline is None or statusline_payload != expected_statusline
    ):
        statusline_errors.append("status line content")

if claude_root == home / ".claude":
    expected_hook_command = "~/.claude/hooks/nameintent_title.sh"
    expected_status_command = "~/.claude/statusline.sh"
else:
    expected_hook_command = str(hook_path)
    expected_status_command = str(statusline_path)

settings_paths = [("default", claude_root / "settings.json")]
accounts_root = Path(os.environ.get("XDG_DATA_HOME") or (home / ".local/share")) / "session-kit" / "accounts" / "claude"
try:
    account_entries = sorted(accounts_root.iterdir(), key=lambda path: path.name)
except OSError:
    account_entries = []
for account_path in account_entries:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", account_path.name):
        continue
    try:
        account_info = account_path.lstat()
    except OSError:
        continue
    if (
        not stat.S_ISDIR(account_info.st_mode)
        or stat.S_ISLNK(account_info.st_mode)
        or account_info.st_uid != os.geteuid()
        or stat.S_IMODE(account_info.st_mode) & 0o002
    ):
        hook_errors.append(f"{account_path.name} unsafe profile")
        statusline_errors.append(f"{account_path.name} unsafe profile")
        continue
    settings_paths.append((account_path.name, account_path / "settings.json"))

for profile, settings_path in settings_paths:
    settings_text = bounded_text(settings_path)
    try:
        settings = json.loads(settings_text or "")
    except ValueError:
        settings = None
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    for event in ("SessionStart", "UserPromptSubmit", "Stop"):
        entries = hooks.get(event) if isinstance(hooks, dict) else None
        covered = False
        if isinstance(entries, list):
            for group in entries:
                if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                    continue
                covered = covered or any(
                    isinstance(hook, dict)
                    and hook.get("type") == "command"
                    and hook.get("command") == expected_hook_command
                    for hook in group["hooks"]
                )
        if not covered:
            hook_errors.append(f"{profile} {event}")
    expected_registration = {
        "type": "command",
        "command": expected_status_command,
        "refreshInterval": 2,
    }
    if not isinstance(settings, dict) or settings.get("statusLine") != expected_registration:
        statusline_errors.append(f"{profile} registration")


def hook_fixture_ok() -> bool:
    if hook_payload is None:
        return False
    fixture_input = json.dumps(
        {"session_id": "doctor-fixture", "hook_event_name": "SessionStart"}
    ).encode()
    with tempfile.TemporaryDirectory(prefix="session-kit-hook-doctor.") as temporary:
        fixture_home = Path(temporary)
        sessions = fixture_home / ".claude" / "sessions"
        sessions.mkdir(parents=True, mode=0o700)
        (sessions / "doctor-fixture.nameintent").write_text(
            "Session Kit Doctor Fixture\n", encoding="utf-8"
        )
        child = None
        payload = bytearray()
        failed = False
        try:
            child = subprocess.Popen(
                [str(hook_path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={
                    "HOME": str(fixture_home),
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "SESSION_KIT_AUTO_NAME": "0",
                },
                start_new_session=True,
            )
            assert child.stdin is not None and child.stdout is not None
            child.stdin.write(fixture_input)
            child.stdin.close()
            descriptor = child.stdout.fileno()
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                ready, _, _ = select.select([descriptor], [], [], 0.1)
                if ready:
                    block = os.read(descriptor, 4097 - len(payload))
                    if not block:
                        break
                    payload.extend(block)
                    if len(payload) > 4096:
                        failed = True
                        break
                elif child.poll() is not None:
                    break
            else:
                failed = True
        except (OSError, ValueError):
            failed = True
        finally:
            if child is not None:
                failed = not terminate_group(child) or failed
                if child.stdout is not None:
                    child.stdout.close()
        if failed or child is None or child.returncode != 0:
            return False
        try:
            record = json.loads(payload.decode("utf-8", "strict"))
        except (UnicodeError, ValueError):
            return False
        return record == {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "sessionTitle": "Session Kit Doctor Fixture",
            }
        }


if not hook_fixture_ok():
    hook_errors.append("functional fixture")
if hook_errors:
    emit("warn", "naming-hook", "Claude title hook coverage missing: " + ", ".join(hook_errors))
else:
    emit("ok", "naming-hook", "Claude title hook matches its installation record and covers every profile")
if statusline_errors:
    emit("warn", "statusline", "Claude status line coverage missing: " + ", ".join(statusline_errors))
else:
    emit("ok", "statusline", "Claude status line matches its installation record and every profile is registered")


# Who owns the terminal tab name (K3). Two things can silence the kit's name
# there, and neither announces itself: the vendor's own off-switch, which the
# live drill proved silences the kit's hook title as well as Claude's own
# writes, and the kit's kill switch.
# Enumerated live against codex-cli 0.145.0, one id per run, with doctor's own
# title check as the oracle. cwd, account and none are deliberately absent:
# Codex rejects all three, and an earlier version of this set listed them
# because its probe read "no error printed" as acceptance.
CODEX_TITLE_ITEMS = {
    "spinner", "activity", "thread", "thread-title", "project", "project-name",
    "model", "model-with-reasoning", "git-branch",
    "context-used", "weekly-limit", "five-hour-limit",
}
tab_notes = []
if os.environ.get("CLAUDE_CODE_DISABLE_TERMINAL_TITLE", "").strip():
    tab_notes.append(
        "CLAUDE_CODE_DISABLE_TERMINAL_TITLE is set, which silences the kit's "
        "title in Claude windows as well as Claude's own"
    )
if os.environ.get("SESSION_KIT_TAB_TITLE", "").strip().casefold() == "off":
    tab_notes.append("SESSION_KIT_TAB_TITLE=off; the kit writes no tab names")
codex_template = codex_home / "session-kit" / "terminal-title.toml"
try:
    codex_home_info = codex_home.lstat()
except OSError:
    codex_home_info = None
if codex_home_info is not None and stat.S_ISLNK(codex_home_info.st_mode):
    # The installer refuses a symlinked Codex home, so "reinstall to deploy it"
    # would be advice that can never succeed. Say what is actually wrong.
    tab_notes.append(
        f"the Codex home {codex_home} is a symlink, which the installer will "
        "not write through; Codex windows keep the default title"
    )
elif codex_home_info is not None and stat.S_ISDIR(codex_home_info.st_mode):
    # Parsed, not pattern-matched: this is the exact file the launcher reads,
    # and it decides between the kit's items and the built-in fallback.
    parsed = None
    def _sk_parse_title_template(text):
        # tomllib when it exists; else the template's own tiny dialect
        # (one [table] level, JSON-compatible values). Python 3.10 has no tomllib.
        try:
            import tomllib
        except ImportError:
            tomllib = None
        if tomllib is not None:
            try:
                return tomllib.loads(text)
            except Exception:
                return None
        import json as _json
        import re as _re
        parsed = {}
        current = None
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            header = _re.fullmatch(r"\[([A-Za-z0-9_-]+)\]", line)
            if header:
                current = parsed.setdefault(header.group(1), {})
                if not isinstance(current, dict):
                    return None
                continue
            pair = _re.fullmatch(r"([A-Za-z0-9_-]+)\s*=\s*(\".*\"|\[.*\])", line)
            if not pair:
                return None
            try:
                value = _json.loads(pair.group(2))
            except ValueError:
                return None
            (current if current is not None else parsed)[pair.group(1)] = value
        return parsed

    try:
        template_bytes = codex_template.read_bytes()
    except OSError:
        template_bytes = b""
    if template_bytes:
        try:
            parsed = _sk_parse_title_template(template_bytes.decode("utf-8"))
        except Exception:
            parsed = None
    if not template_bytes:
        tab_notes.append(
            "the Codex tab-title template is missing; Codex windows keep the "
            "default title (reinstall to deploy it)"
        )
    elif parsed is None:
        tab_notes.append(
            "the Codex tab-title template is not valid TOML, so kit sessions "
            "fall back to the built-in title items"
        )
    else:
        tui = parsed.get("tui")
        if not isinstance(tui, dict):
            tab_notes.append(
                "the Codex tab-title template is malformed: tui must be a table"
            )
        else:
            items = tui.get("terminal_title")
            if not isinstance(items, list) or not items:
                tab_notes.append("the Codex tab-title template names no items")
            else:
                unknown = [
                    str(item) for item in items if str(item) not in CODEX_TITLE_ITEMS
                ]
                if unknown:
                    tab_notes.append(
                        "the Codex tab-title template names items Codex does not "
                        "know: " + ", ".join(sorted(unknown))
                    )
                elif "thread" not in items and "thread-title" not in items:
                    tab_notes.append(
                        "the Codex tab-title template does not include the thread "
                        "name, so Codex tabs will not carry the session name"
                    )
if tab_notes:
    emit("warn", "tab-title", "; ".join(tab_notes))
else:
    emit("ok", "tab-title", "the kit owns the tab name on both providers")


# Whether Codex sessions can still be named at all. The titler runs before
# every human-facing inventory build, through a caller that sends both its
# streams to /dev/null, so a store it cannot read produced no name and no word
# about it. It leaves the reason here instead; a healthy pass removes the file.
titler_failure = (
    Path(
        os.environ.get("SESSION_KIT_STATE_DIR")
        or os.path.join(
            os.environ.get("XDG_STATE_HOME") or os.path.join(str(home), ".local/state"),
            "session-kit",
        )
    )
    / "codex-autotitle-error.json"
)
try:
    if titler_failure.is_symlink():
        raise OSError("refusing a symlinked record")
    record = json.loads(titler_failure.read_text(encoding="utf-8"))
except (OSError, ValueError):
    record = None
if isinstance(record, dict) and record.get("detail"):
    when = str(record.get("at") or "")
    emit(
        "warn",
        "codex-titles",
        f"Codex sessions are not being named: {record['detail']}"
        + (f" (last seen {when})" if when else ""),
    )
else:
    emit("ok", "codex-titles", "Codex sessions are being named")

# The attention hook is how the picker learns that a Claude session is waiting
# for a person at the moment it starts waiting. Not installed is not a failure
# -- the poll still finds waiting sessions on its own schedule -- but it has to
# be VISIBLE, because a hook that is registered and broken looks exactly like
# an estate where nobody is waiting.
attention_errors = []
attention_path = home / ".claude" / "hooks" / "attention_hook.sh"
attention_payload = secure_regular_bytes(attention_path, 1_048_576, executable=True)
if attention_payload is None:
    attention_errors.append("hook file")
attention_commands = {
    "~/.claude/hooks/attention_hook.sh",
    str(attention_path),
}
attention_events = []
for event in ("Notification", "UserPromptSubmit", "SessionEnd"):
    entries = hooks.get(event) if isinstance(hooks, dict) else None
    covered = False
    if isinstance(entries, list):
        for group in entries:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                continue
            for hook in group["hooks"]:
                if (
                    isinstance(hook, dict)
                    and hook.get("type") == "command"
                    and isinstance(hook.get("command"), str)
                    and hook["command"] in attention_commands
                ):
                    covered = True
    if not covered:
        attention_events.append(event)


def attention_fixture_ok() -> bool:
    """Feed it one real Notification and read the record back."""
    if attention_payload is None:
        return False
    session_id = "00000000-0000-4000-8000-00000000d0c7"
    fixture_input = json.dumps(
        {
            "session_id": session_id,
            "hook_event_name": "Notification",
            "notification_type": "permission_prompt",
            "message": "doctor fixture",
        }
    ).encode()
    with tempfile.TemporaryDirectory(prefix="session-kit-attention-doctor.") as temporary:
        fixture_home = Path(temporary)
        state_dir = fixture_home / "state"
        try:
            child = subprocess.run(
                [str(attention_path)],
                input=fixture_input,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={
                    "HOME": str(fixture_home),
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "SESSION_KIT_STATE_DIR": str(state_dir),
                },
                timeout=10,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            return False
        if child.returncode != 0:
            return False
        record_path = state_dir / "attention" / "claude" / f"{session_id}.json"
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return record.get("needs_you") is True and record.get("schema_version") == 1


if attention_payload is not None and attention_events:
    attention_errors.append("not registered for: " + ", ".join(attention_events))
if attention_payload is not None and not attention_fixture_ok():
    attention_errors.append("functional fixture")
if attention_payload is None:
    emit(
        "warn",
        "attention-hook",
        "Claude attention hook is not installed; waiting sessions are found by the poll only",
    )
elif attention_errors:
    emit("warn", "attention-hook", "Claude attention hook: " + "; ".join(attention_errors))
else:
    emit(
        "ok",
        "attention-hook",
        "Claude attention hook covers Notification, UserPromptSubmit, and SessionEnd",
    )


active = []
_off_words = {"0", "false", "no", "off"}
if os.environ.get("SESSION_KIT_AUTO_NAME", "").strip().casefold() in _off_words:
    active.append("SESSION_KIT_AUTO_NAME")
# The picker's live channels default on; each is switched off by the same
# words (0/off/no/false, any case, spaces trimmed) its own reader honours, so
# doctor reports a disabled channel instead of an "all clear" over one.
for name in (
    "SESSION_KIT_PICKER_EVENTS",
    "SESSION_KIT_PICKER_PULSE",
    "SESSION_KIT_CLAUDE_SOCKET",
):
    if os.environ.get(name, "").strip().casefold() in _off_words:
        active.append(name)
for name in ("SESSION_KIT_NO_COLOR", "NO_COLOR"):
    if name in os.environ:
        active.append(name)
for name in (".no_shpool_reaper", ".no_shpool_watchdog"):
    if (home / name).exists() or (home / name).is_symlink():
        active.append(name)
# The sub-agent sweep ends processes every five minutes and carries switches of
# its own -- one of them a FILE, because the systemd user manager inherits none
# of their shell environment, so the variable never reaches the pass that closes
# things. Reporting "no supported kill switch is active" while one of these was
# holding would be the same false all-clear the sentinel regression produced.
if os.environ.get("SESSION_KIT_SUBAGENT_SWEEP", "").strip().casefold() in _off_words:
    active.append("SESSION_KIT_SUBAGENT_SWEEP")
_sweep_off_file = kit_state_root / "subagent-sweep-off"
if _sweep_off_file.exists() or _sweep_off_file.is_symlink():
    active.append("<state>/subagent-sweep-off")
if active:
    emit("warn", "kill-switches", "active: " + ", ".join(active))
else:
    emit("ok", "kill-switches", "no supported kill switch is active")


# The sub-agent sweep, reported as a STATE rather than as a unit file.
#
# Doctor already checks that the timer is enabled and active, and that is not
# the same question as "is the rule being applied". A held state-directory
# lock, a crashed pass, a window nobody can read, or an off switch all retire
# the fifteen-minute rule while the timer stays green -- and a sweep that had
# gone quiet used to leave no evidence anywhere (lane B F9). The durable trace
# is `last_pass` in the sweep's own state file, which only a pass that ran to
# completion writes.
#
# CRUCIAL: this reports what the TIMER gets, not what this shell has. The
# systemd user manager inherits none of the operator's environment, so
# SESSION_KIT_SUBAGENT_IDLE_MINUTES reaches a hand-run pass and NOTHING ELSE.
# Reading the window out of `os.environ` would have printed
# "ok ... 120-minute window" on a machine whose timer was closing at fifteen --
# a green line understating the closer by 8x. The variable is still reported,
# as the warning it is.
#
# The file reader mirrors subagent_sweep._env_idle_hours' file branch and the
# environment reader mirrors its variable branch; tests/test_subagent_sweep.py
# pins both against the same matrix so they cannot drift apart.
def sweep_usable_duration(raw, per_unit):
    """A window in minutes, or None for anything that is not a duration."""
    try:
        value = float(raw)
    except ValueError:
        return None
    if value < 0 or value != value or value == float("inf"):
        return None
    return value * per_unit


def sweep_window_from_file():
    """What the TIMER resolves: the state-directory file, then the default."""
    window_file = kit_state_root / "subagent-sweep-minutes"
    try:
        if window_file.is_file() and not window_file.is_symlink():
            # Bytes, then a replacing decode. `read_text` decodes strictly, and
            # a single non-UTF-8 byte in this operator-editable file raised
            # UnicodeDecodeError -- a ValueError, not an OSError -- straight out
            # of this program, which made the bash caller discard EVERY extended
            # check, not just this one. One stray byte blinded the whole audit.
            raw = window_file.read_bytes()[:64].decode("utf-8", "replace").strip()
            if raw:
                value = sweep_usable_duration(raw, 1.0)
                if value is None:
                    return None, "<state>/subagent-sweep-minutes contains " + raw
                return value, "<state>/subagent-sweep-minutes"
    except OSError:
        pass
    return 15.0, "the built-in default"


def sweep_window_from_env():
    """What a HAND-RUN pass resolves, or (None, "") when no variable is set.

    Never what the timer resolves. That is the entire point of reporting it.
    """
    for name, per_unit in (
        ("SESSION_KIT_SUBAGENT_IDLE_MINUTES", 1.0),
        ("SESSION_KIT_SUBAGENT_IDLE_HOURS", 60.0),
    ):
        if name not in os.environ:
            continue
        raw = (os.environ.get(name) or "").strip()
        if not raw:
            return None, name + " is set but empty"
        value = sweep_usable_duration(raw, per_unit)
        if value is None:
            return None, name + "=" + raw + " is not a usable duration"
        return value, name
    return None, ""


_sweep_state = kit_state_root / "subagent-sweep.state.json"
_sweep_minutes, _sweep_source = sweep_window_from_file()
_env_minutes, _env_source = sweep_window_from_env()
# The reaper resolves its brake as SESSION_KIT_REAPER_SENTINEL, falling back to
# ~/.no_shpool_reaper (bin/shpool_reaper:31). Hardcoding the fallback would
# report the brake ENGAGED while a redirected sentinel left the sweep running.
_sentinel = Path(os.environ.get("SESSION_KIT_REAPER_SENTINEL") or (home / ".no_shpool_reaper"))
_sentinel_name = (
    "SESSION_KIT_REAPER_SENTINEL (" + str(_sentinel) + ")"
    if os.environ.get("SESSION_KIT_REAPER_SENTINEL")
    else "~/.no_shpool_reaper"
)
_sweep_off_reason = ""
if _sentinel.exists() or _sentinel.is_symlink():
    _sweep_off_reason = _sentinel_name
elif "<state>/subagent-sweep-off" in active:
    _sweep_off_reason = "<state>/subagent-sweep-off"
elif _sweep_minutes is None:
    _sweep_off_reason = "a window it cannot read: " + _sweep_source
elif _sweep_minutes == 0:
    _sweep_off_reason = "a window of 0 from " + _sweep_source
# An exported variable does not reach the timer, so it is a caveat on every
# verdict below rather than a source for any of them.
_env_note = ""
if _env_source and _env_minutes is None:
    _env_note = (
        "; " + _env_source + ", which does not reach the timer anyway"
        " -- put a number of minutes in <state>/subagent-sweep-minutes instead"
    )
elif _env_source and _env_minutes != _sweep_minutes:
    _env_note = (
        "; %s=%g is set in this shell but does NOT reach the timer"
        " -- put it in <state>/subagent-sweep-minutes instead"
        % (_env_source, _env_minutes)
    )
if active_platform in ("macos", "Darwin"):
    # `platform()` reports `macos`; `Darwin` is only ever seen through
    # SESSION_KIT_TEST_PLATFORM. Both are accepted so this cannot report a
    # permanently quiet sweep on a machine where it correctly never runs.
    emit("ok", "subagent-sweep", "the sub-agent sweep does not run on macOS")
elif _sweep_off_reason:
    # Off is a legitimate answer and must never read as an all-clear.
    emit(
        "warn",
        "subagent-sweep",
        "the sub-agent sweep is OFF, disabled by " + _sweep_off_reason
        + "; finished sub-agent workers are not being closed" + _env_note,
    )
else:
    _window_text = "%g-minute window from %s" % (_sweep_minutes, _sweep_source)
    # Everything that reads the state file lives in one try: a hand-edited
    # `last_pass` of -Infinity or a 400-digit integer would otherwise raise
    # ValueError or OverflowError out of this program and discard the whole
    # extended audit, exactly as a stray byte in the window file did.
    try:
        _sweep_payload = json.loads(_sweep_state.read_bytes().decode("utf-8", "replace"))
        _last_pass = _sweep_payload.get("last_pass")
        if isinstance(_last_pass, bool) or not isinstance(_last_pass, (int, float)):
            _age = None
        else:
            _last_pass = float(_last_pass)
            _age = time.time() - _last_pass
            if _age != _age or _age in (float("inf"), float("-inf")):
                _age = None
    except (OSError, ValueError, AttributeError, OverflowError):
        _age = None
    if _age is None:
        # Expected for the first couple of minutes after an install (the timer
        # is OnBootSec=2min) and after an upgrade from a release that did not
        # record it. Persisting past that means the pass is not running.
        emit(
            "warn",
            "subagent-sweep",
            _window_text + ", but no completed pass is recorded yet in "
            + str(_sweep_state) + "; expected right after an install or "
            "upgrade, and a sign the pass is not running if it persists"
            + _env_note,
        )
    # The timer is every five minutes with AccuracySec=30s. Four cadences of
    # slack, so an ordinary overlapping or skipped pass never cries wolf and a
    # sweep that has actually stopped is named within twenty minutes.
    elif _age > 20 * 60:
        emit(
            "warn",
            "subagent-sweep",
            _window_text + ", but the last completed pass was "
            + str(int(_age // 60)) + " minutes ago; the sweep has gone quiet"
            " (a held state-directory lock, a crashed pass, or a timer that"
            " is not firing)" + _env_note,
        )
    elif _age < 0:
        emit(
            "warn",
            "subagent-sweep",
            _window_text + ", but the last completed pass is dated in the"
            " future; the clock moved and the sweep will restart its clocks"
            + _env_note,
        )
    elif _env_note:
        emit("warn", "subagent-sweep", _window_text + _env_note)
    else:
        emit(
            "ok",
            "subagent-sweep",
            _window_text + "; last completed pass " + str(int(_age // 60)) + "m ago",
        )

acceptance = config_root / "release-acceptance.json"
try:
    acceptance_payload = secure_regular_bytes(acceptance, 16_384, exact_mode=0o600)
    if acceptance_payload is None:
        raise ValueError
    record = json.loads(acceptance_payload.decode("utf-8", "strict"))
    if not isinstance(record, dict) or set(record) != {
        "schema_version",
        "release_id",
        "platform",
        "provider_versions",
        "accepted_on",
        "evidence",
    }:
        raise ValueError
    versions = record.get("provider_versions")
    evidence = record.get("evidence")
    accepted_on = record.get("accepted_on")
    if (
        record.get("schema_version") != 1
        or record.get("release_id") != active_release
        or record.get("platform") != active_platform
        or not isinstance(versions, dict)
        or set(versions) != {"claude", "codex"}
        or not all(provider_ready.values())
        or versions != provider_observed
        or not isinstance(accepted_on, str)
        or not isinstance(evidence, dict)
        or set(evidence) != {"unique_colors", "thread_titles", "resume_roundtrip"}
        or not all(
            isinstance(value, str)
            and 1 <= len(value) <= 256
            and "\n" not in value
            and "\r" not in value
            for value in evidence.values()
        )
    ):
        raise ValueError
    accepted_date = datetime.date.fromisoformat(accepted_on)
    if accepted_date > datetime.date.today():
        raise ValueError
except (OSError, UnicodeError, ValueError):
    emit(
        "warn",
        "acceptance",
        "this release has no valid private acceptance record; follow the "
        "release-acceptance.json schema in docs/troubleshooting.md",
    )
else:
    emit("ok", "acceptance", "private release acceptance record is present")

# Vendor internal formats the kit reads or writes. Both vendors document these
# files as internal and version-unstable, and the kit depends on five of them
# (docs/pinned-internal-formats.md names every reader). A format that changes
# shape does not announce itself: the reader simply finds nothing, and a
# session's title, history or colour quietly stops working. This check reads one
# live example of each and says which assumption it could not confirm.
# A home directory on slow or stalled storage must not turn doctor into a hang:
# the scan gets a wall-clock budget and every traversal below is bounded by it.
try:
    format_budget = float(os.environ.get("SESSION_KIT_DOCTOR_FORMAT_SECONDS", "10"))
except ValueError:
    format_budget = 10.0
format_deadline = time.monotonic() + max(0.0, format_budget)
format_verdicts = []


def out_of_time() -> bool:
    return time.monotonic() >= format_deadline


def json_lines(path, limit: int = 262_144, cap: int = 50):
    """(records, why-not) from the COMPLETE lines at the head of a file.

    A file with no complete line is not a moved format: it is a transcript a
    session created a moment ago, or one whose first line is longer than the
    window. Doctor's loudest verdict must never come from a file that is
    simply still being written.
    """
    try:
        with open(path, "rb") as handle:
            block = handle.read(limit)
    except OSError:
        return [], "unreadable"
    if b"\n" not in block:
        return [], "no complete line yet"
    records = []
    for line in block.split(b"\n")[:-1]:
        if not line.strip():
            continue
        try:
            records.append(json.loads(line.decode("utf-8", "strict")))
        except (UnicodeError, ValueError):
            if not records:
                return [], "its first line is no longer JSON"
            break
        if len(records) >= cap:
            break
    if not records:
        return [], "no complete line yet"
    return records, ""


def newest(paths, count=1):
    ranked = []
    for path in paths:
        if out_of_time():
            break
        try:
            ranked.append((path.stat().st_mtime, path))
        except OSError:
            continue
    ranked.sort(reverse=True)
    return [path for _, path in ranked[:count]]


def bounded_glob(root, pattern, cap=2000):
    """Materialise at most `cap` paths, and stop when the clock says stop."""
    found = []
    try:
        for path in root.glob(pattern):
            found.append(path)
            if len(found) >= cap or out_of_time():
                break
    except OSError:
        return []
    return found


def record_verdict(name, state, detail):
    format_verdicts.append((name, state, detail))


def judge_fields(record, required, optional=()):
    """(state, detail). The exact fields the kit reads, named as they go."""
    if not isinstance(record, dict):
        return "moved", "is no longer a JSON object"
    missing = [field for field in required if field not in record]
    if missing:
        return "moved", "lost " + ", ".join(missing)
    thin = [field for field in optional if field not in record]
    if thin:
        return "thin", "carries no " + ", ".join(thin)
    return "ok", ""


def check_json_object(name, path, required, optional=()):
    """One live example of a whole-file JSON object, field by field."""
    text = bounded_text(path, 262_144)
    if text is None:
        # Unreadable, oversized or rewritten under us: no evidence either way.
        record_verdict(name, "absent", "the live example could not be read whole")
        return
    try:
        record = json.loads(text)
    except ValueError:
        record_verdict(name, "moved", "no longer parses as JSON")
        return
    record_verdict(name, *judge_fields(record, required, optional))


def check_json_lines(name, path, required, optional=()):
    """One live example of a JSON-lines file. These files carry several kinds
    of line, so the verdict is the best any complete head line earns: only a
    file where NO line still has the shape counts as moved."""
    records, why = json_lines(path)
    if not records:
        record_verdict(name, "absent" if why == "no complete line yet" else "moved", why)
        return
    objects = [record for record in records if isinstance(record, dict)]
    if not objects:
        record_verdict(name, "moved", "records are no longer JSON objects")
        return
    verdicts = [judge_fields(record, required, optional) for record in objects]
    for state in ("ok", "thin"):
        for candidate in verdicts:
            if candidate[0] == state:
                record_verdict(name, *candidate)
                return
    record_verdict(name, *verdicts[0])


claude_sessions = home / ".claude" / "sessions"
records = newest(
    [
        path
        for path in bounded_glob(claude_sessions, "*.json")
        if path.name[:-5].isdigit()
    ]
)
if not records:
    record_verdict("Claude session record", "absent", "no live example to check")
else:
    # docs/pinned-internal-formats.md names these: sessionId identifies the
    # conversation, messagingSocketPath is the whole of socket delivery, and
    # statusUpdatedAt is the attention merge's only tie-breaker. The first two
    # break a feature outright; the last two degrade it, so they warn.
    check_json_object(
        "Claude session record",
        records[0],
        required=("sessionId", "pid"),
        optional=("messagingSocketPath", "statusUpdatedAt"),
    )

transcripts = newest(bounded_glob(home / ".claude" / "projects", "*/*.jsonl"), 3)
if not transcripts:
    record_verdict("Claude transcript", "absent", "no live example to check")
else:
    for position, transcript in enumerate(transcripts):
        before = len(format_verdicts)
        # `type` is what every transcript reader dispatches on: ai-title,
        # agent-name, agent-color.
        check_json_lines("Claude transcript", transcript, required=("type",))
        if format_verdicts[before][1] != "absent" or position == len(transcripts) - 1:
            break
        del format_verdicts[before:]

keys = newest(bounded_glob(claude_sessions, "*.key"))
if not keys:
    record_verdict("Claude messaging key", "absent", "no live example to check")
else:
    # claude_socket.read_peer_token sends nothing without peerToken.
    check_json_object("Claude messaging key", keys[0], required=("peerToken",))

codex_sessions = codex_home / "sessions"
rollouts = newest(bounded_glob(codex_sessions, "**/rollout-*.jsonl"))
if not rollouts:
    record_verdict("Codex rollout", "absent", "no live example to check")
else:
    # providers_codex reads event.type and the payload mapping under it;
    # transcript_text.render_rollout reads the same pair.
    check_json_lines("Codex rollout", rollouts[0], required=("type", "payload"))

index = codex_home / "session_index.jsonl"
if not index.is_file():
    record_verdict("Codex session index", "absent", "no live example to check")
else:
    # read_codex_session_index keeps an entry only when it has both.
    check_json_lines(
        "Codex session index", index, required=("id",), optional=("thread_name",)
    )

format_moved = [
    f"{name} {detail}" for name, state, detail in format_verdicts if state == "moved"
]
format_thin = [
    f"{name} {detail}" for name, state, detail in format_verdicts if state == "thin"
]
format_absent = [name for name, state, _ in format_verdicts if state == "absent"]
format_ok = [name for name, state, _ in format_verdicts if state == "ok"]
if format_moved:
    # A shape that has already changed is not a warning: something a person
    # depends on is broken right now, and every moved format is named.
    emit("fail", "internal-formats", "; ".join(format_moved))
elif format_thin:
    # The file is still there and still readable; one field the kit reads is
    # gone, so the feature that reads it degrades rather than breaks.
    emit("warn", "internal-formats", "; ".join(format_thin))
elif out_of_time():
    emit(
        "warn",
        "internal-formats",
        "the vendor format check ran out of time before it finished",
    )
elif format_absent:
    emit(
        "warn",
        "internal-formats",
        f"{len(format_ok)} of {len(format_verdicts)} pinned vendor formats have the "
        "shape the kit reads; no live example of: " + ", ".join(format_absent),
    )
else:
    emit(
        "ok",
        "internal-formats",
        f"all {len(format_ok)} pinned vendor formats still have the shape the kit reads",
    )
PY
  ); then
    while IFS=$'\t' read -r status name detail; do
      if [[ ${audit_expected[$name]:-0} == 1 && -z ${audit_seen[$name]:-} &&
            $status =~ ^(ok|warn|fail)$ && -n $detail ]]; then
        add_check "$status" "$name" "$detail"
        audit_seen[$name]=1
      elif [[ -n $status || -n $name || -n $detail ]]; then
        audit_ok=0
      fi
    done <<< "$audit_output"
    for audit_name in "${audit_names[@]}"; do
      [[ ${audit_seen[$audit_name]:-0} == 1 ]] || audit_ok=0
    done
  else
    audit_ok=0
  fi
  (( audit_ok == 1 )) || add_check warn migration-audit \
    "extended read-only audit failed or returned an incomplete check set"
  for helper in "${helpers[@]}"; do
    if [[ -x $bin_dir/$helper ]]; then
      add_check ok "$helper" "$bin_dir/$helper"
    else
      add_check fail "$helper" "launcher unavailable"
    fi
  done
  if [[ -f $config_root/inventory.json && ! -L $config_root/inventory.json ]] &&
      python3 -m json.tool "$config_root/inventory.json" >/dev/null 2>&1; then
    add_check ok config "inventory.json is valid JSON"
  else
    add_check fail config "inventory.json is missing, unsafe, or invalid"
  fi
  if (
    validate_purge_ownership \
      install "$install_root" "$install_root/.session-kit-owned.json" &&
    validate_purge_ownership \
      config "$config_root" "$config_root/.session-kit-owned.json"
  ) >/dev/null 2>&1; then
    add_check ok ownership "receipt and root ownership markers match"
  else
    add_check fail ownership "receipt or root ownership marker is missing or incompatible"
  fi
  # Session recordings are the largest thing the kit writes and the only feed
  # with no policy at all: they are removed as a side effect of pruning or
  # closing the owning session, and never aged out. Measured at 2.6 GB on a box
  # two weeks old, 470 MB of it belonging to sessions that no longer exist.
  # Reported, with a bound the operator can set -- never deleted here: a
  # recording is the history `sp find` and `sp history` read, and nothing should
  # remove one on a health check.
  local journal_days=${SESSION_KIT_JOURNAL_RETENTION_DAYS:-30}
  [[ $journal_days =~ ^[0-9]+$ ]] || journal_days=30
  local journal_report=
  journal_report=$(python3 - "$journal_dir" "$journal_days" 2>/dev/null <<'PY'
import os
import sys
import time

root, days_text = sys.argv[1:3]
days = int(days_text)
if not os.path.isdir(root):
    print("ok\tno session recordings are stored")
    raise SystemExit(0)
cutoff = time.time() - days * 86400
total = older = 0
oldest = time.time()
for base, _directories, files in os.walk(root):
    for name in files:
        path = os.path.join(base, name)
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if not os.path.isfile(path):
            continue
        total += info.st_size
        oldest = min(oldest, info.st_mtime)
        if info.st_mtime < cutoff:
            older += info.st_size


def size(value):
    for unit in ("B", "KB", "MB"):
        if value < 1024:
            return f"{value:.0f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


age = max(0, int((time.time() - oldest) / 86400))
if older:
    print(
        f"warn\tsession recordings are {size(total)}, of which {size(older)} is "
        f"older than {days} days; closing or pruning those sessions is what "
        f"reclaims it, and SESSION_KIT_JOURNAL_RETENTION_DAYS sets the bound "
        f"this check uses"
    )
else:
    print(f"ok\tsession recordings are {size(total)}, none older than {days} days (oldest {age}d)")
PY
) || journal_report=""
  if [[ -n $journal_report ]]; then
    local journal_status= journal_detail=
    journal_report=${journal_report//$'\t'/$'\034'}
    IFS=$'\034' read -r journal_status journal_detail <<<"$journal_report"
    case $journal_status in
      ok | warn) add_check "$journal_status" journals "$journal_detail" ;;
      *) add_check warn journals "session recordings could not be measured" ;;
    esac
  else
    add_check warn journals "session recordings could not be measured"
  fi
  if [[ -e $transaction_path || -L $transaction_path ]]; then
    add_check fail transaction "interrupted lifecycle transaction requires recovery"
  else
    add_check ok transaction "no interrupted lifecycle transaction"
  fi
  if [[ -f $integration_marker && ! -L $integration_marker &&
        $(stat_mode "$integration_marker") == 600 ]]; then
    add_check ok login "enabled with a private release marker"
  else
    add_check warn login "disabled or not yet validated"
  fi
  if (( json )); then
    printf '%s\n' "${checks[@]}" | python3 -c '
import json,sys
rows=[]
for line in sys.stdin:
    status,name,detail=line.rstrip("\n").split("\t",2)
    rows.append({"status":status,"name":name,"detail":detail})
print(json.dumps({"ok":all(r["status"]!="fail" for r in rows),"checks":rows},indent=2,sort_keys=True))
'
  else
    for row in "${checks[@]}"; do
      IFS=$'\t' read -r status name detail <<<"$row"
      render_check_row "$status" "$name" "$detail"
    done
  fi
  (( failed == 0 ))
}
