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
  local transport=$1
  shift
  if [[ $transport == machine ]]; then
    systemctl --user --machine=@.host "$@"
  else
    systemctl --user "$@"
  fi
}

# Report what systemd says about the Session Kit units, not what the file
# system says. `session-kit services enable` installs unit files for every
# unit, so a file-exists check passes on a machine where the watchdog has
# never run and dead terminals are never recovered.
#
# Called from doctor_command(), which owns add_check() and the checks array.
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
  [[ ${SESSION_KIT_TESTING:-0} != 1 && $transport == direct ]] || probe=0
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
      "installed but not enabled (${file_state:-unknown}), so dead terminals are never recovered; enable it with: $fix"
  elif [[ $active_state != active ]]; then
    add_check fail watchdog \
      "enabled but not running ($active_state), so dead terminals are never recovered; start it with: $fix"
  else
    add_check ok watchdog "enabled and running"
  fi
  # The socket and the timer carry the daemon and the reaper. The two units
  # they activate are reported for context and never required to be running.
  for unit in shpool.socket shpool-reaper.timer shpool.service shpool-reaper.service; do
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
  local linger_status= linger_detail=
  if (( probe == 0 )); then
    add_check warn linger \
      "lingering was not checked here; without it logind stops the user manager at logout and every managed session ends with it"
  else
    IFS=$'\t' read -r linger_status linger_detail < <(logind_linger_report)
    add_check "$linger_status" linger "$linger_detail"
  fi
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
    add_check fail platform "unsupported operating system"
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
    required_commands+=(flock journalctl systemctl)
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
    add_check fail provider "install Claude Code, Codex, or both"
  fi
  local audit_output audit_ok=1 audit_name
  local -a audit_names=(
    claude-version codex-version codex-themes naming-instructions naming-hook
    provider-hooks hook-files
    kill-switches acceptance shpool-binary
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
hook_path = home / ".claude" / "hooks" / "nameintent_title.sh"
hook_payload = secure_regular_bytes(hook_path, 1_048_576, executable=True)
if hook_payload is None:
    hook_errors.append("hook file")
settings_text = bounded_text(home / ".claude" / "settings.json")
try:
    settings = json.loads(settings_text or "")
except ValueError:
    settings = None
hooks = settings.get("hooks") if isinstance(settings, dict) else None
expected_commands = {
    "~/.claude/hooks/nameintent_title.sh",
    str(hook_path),
}
for event in ("SessionStart", "UserPromptSubmit", "Stop"):
    entries = hooks.get(event) if isinstance(hooks, dict) else None
    valid = isinstance(entries, list)
    covered = False
    if valid:
        for group in entries:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                valid = False
                break
            for hook in group["hooks"]:
                if not isinstance(hook, dict):
                    valid = False
                    break
                if (
                    hook.get("type") == "command"
                    and isinstance(hook.get("command"), str)
                    and hook["command"] in expected_commands
                ):
                    covered = True
            if not valid:
                break
    if not valid or not covered:
        hook_errors.append(event)


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
    emit("ok", "naming-hook", "Claude title hook covers SessionStart, UserPromptSubmit, and Stop")


def owned_intake_hook(document, expected_command: str) -> bool:
    if not isinstance(document, dict):
        return False
    all_hooks = document.get("hooks")
    event_groups = all_hooks.get("UserPromptSubmit") if isinstance(all_hooks, dict) else None
    if not isinstance(event_groups, list):
        return False
    owned = []
    for group in event_groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            return False
        for hook in group["hooks"]:
            if not isinstance(hook, dict):
                return False
            provenance = hook.get("sessionKitProvenance")
            if isinstance(provenance, dict) and provenance.get("owner") == "session-kit":
                owned.append(hook)
    return len(owned) == 1 and owned[0].get("command") == expected_command


codex_hooks_text = bounded_text(codex_home / "hooks.json")
try:
    codex_hooks_document = json.loads(codex_hooks_text or "")
except ValueError:
    codex_hooks_document = None
provider_hook_errors = []
if not owned_intake_hook(
    settings,
    '"$HOME/.local/lib/session-kit/current/extras/hooks/sk_session_events.py"',
):
    provider_hook_errors.append("Claude")
if not owned_intake_hook(
    codex_hooks_document,
    'python3 "$HOME/.local/lib/session-kit/current/lib/sessionkit_supervisor/provider_hooks.py" codex-hook',
):
    provider_hook_errors.append("Codex")
provider_hooks_required = (
    install_root / "releases" / active_release / "config" / "codex" / "hooks.json"
).is_file()
if provider_hook_errors and provider_hooks_required:
    emit(
        "fail",
        "provider-hooks",
        "automatic intake hook missing or ambiguous for: "
        + ", ".join(provider_hook_errors),
    )
elif provider_hook_errors:
    emit("ok", "provider-hooks", "automatic intake hooks are not required by the active release")
else:
    emit("ok", "provider-hooks", "Claude and Codex automatic intake hooks are registered once")

# A registered hook command is not a working hook. The release payload is
# assembled from a file list, and a release built without extras/ installs
# cleanly, registers both intake hooks, and then fails on every prompt because
# the command names a file that was never shipped. Check the exact paths the
# registered commands run, not the registration strings.
default_install_root = home / ".local/lib/session-kit"
hook_file_errors = []
for label, relative, executable_required in (
    ("Claude events hook", "extras/hooks/sk_session_events.py", True),
    ("Codex intake hook", "extras/hooks/sk_codex_intake.py", True),
    ("provider hook tool", "lib/sessionkit_supervisor/provider_hooks.py", False),
):
    hook_file = install_root / "current" / relative
    try:
        hook_info = hook_file.stat()
    except OSError:
        hook_file_errors.append(f"{label} is missing ({relative})")
        continue
    if not stat.S_ISREG(hook_info.st_mode):
        hook_file_errors.append(f"{label} is not a regular file ({relative})")
    elif executable_required and not hook_info.st_mode & 0o111:
        hook_file_errors.append(f"{label} is not executable ({relative})")
if install_root != default_install_root:
    # Both registered commands hard-code the default root, so a custom install
    # root leaves them pointing at a tree this install never wrote.
    hook_file_errors.append(
        "the registered hook commands name "
        + os.fspath(default_install_root)
        + ", which is not this install root"
    )
if not provider_hooks_required:
    emit("ok", "hook-files", "the active release does not require intake hook files")
elif hook_file_errors:
    emit(
        "fail",
        "hook-files",
        "; ".join(hook_file_errors)
        + "; install a release that ships them with: session-kit update",
    )
else:
    emit("ok", "hook-files", "every registered hook command resolves to a runnable file")

active = []
if os.environ.get("SESSION_KIT_AUTO_NAME", "").strip().casefold() in {"0", "false", "no", "off"}:
    active.append("SESSION_KIT_AUTO_NAME")
for name in ("SESSION_KIT_NO_COLOR", "NO_COLOR"):
    if name in os.environ:
        active.append(name)
for name in (".no_shpool_reaper", ".no_shpool_watchdog"):
    if (home / name).exists() or (home / name).is_symlink():
        active.append(name)
if active:
    emit("warn", "kill-switches", "active: " + ", ".join(active))
else:
    emit("ok", "kill-switches", "no supported kill switch is active")

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
    emit("warn", "acceptance", "private release-acceptance.json record is missing or invalid")
else:
    emit("ok", "acceptance", "private release acceptance record is present")
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
      printf '%-5s %-18s %s\n' "${status^^}" "$name" "$detail"
    done
  fi
  (( failed == 0 ))
}
