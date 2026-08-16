#!/usr/bin/env bash
# Session Kit preflight and safety checks. Source this file; do not execute it.
#
# Source order: bin/session-kit sources this module after it assigns the
# lifecycle roots (install_root, bin_dir, config_root, state_root,
# service_root), the shell integration paths, receipt_path, helpers,
# common_required_commands, and inventory_files, and after it defines die(),
# platform(), and require_arg(). These functions also call
# transaction_targets() and current_release_id() from
# lib/sh/session_kit_lifecycle.sh, which only has to be sourced before the
# dispatcher runs.
#
# Globals the entry script owns are assigned there, not here.
# shellcheck disable=SC2154

# One row, one layout, for every report the kit prints. `session-kit doctor`
# and `session-kit check` are the two reports a person runs when something is
# wrong, so they render the same way and name each check the same way.
render_check_row() {
  printf '%-5s %-18s %s\n' "${1^^}" "$2" "$3"
}

systemd_user_manager_transport() {
  if systemctl --user show-environment >/dev/null 2>&1; then
    printf '%s\n' direct
    return 0
  fi
  if systemctl --user --machine=@.host show-environment \
      >/dev/null 2>&1; then
    printf '%s\n' machine
    return 0
  fi
  return 1
}

# Report the installed shpool against the release the kit targets.
#
# Every place the kit names a shpool version names 0.11.0: install.sh, the
# macOS install path (which refuses anything else), docs/install.md,
# macos/README.md, extras/build-static-binary.md, and the shpool-patch series,
# which applies to upstream v0.11.0 only. No other release is tested against
# the kit's `shpool list --json` use or the config keys it installs, so an
# older binary is a failure and a newer one an untested warning rather than a
# silent pass.
#
# Prints one "<status>\t<detail>" line; status is ok, warn, or fail.
shpool_version_report() {
  python3 - 0.11.0 <<'PY'
import re
import shutil
import subprocess
import sys

minimum = sys.argv[1]
floor = tuple(int(part) for part in minimum.split("."))
fix = f"cargo install shpool --version {minimum} --locked"


def emit(status: str, detail: str) -> None:
    print(status, " ".join(detail.split()), sep="\t")


executable = shutil.which("shpool")
if executable is None:
    emit("fail", f"shpool is not on PATH; install it with: {fix}")
    raise SystemExit(0)
text = ""
for argv in ([executable, "version"], [executable, "--version"]):
    try:
        done = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        continue
    candidate = done.stdout.decode("utf-8", "replace")[:200]
    if re.search(r"\d+\.\d+\.\d+", candidate):
        text = candidate
        break
match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
if match is None:
    emit(
        "warn",
        f"the installed shpool did not report a version; the kit targets {minimum}: {fix}",
    )
    raise SystemExit(0)
found = tuple(int(part) for part in match.groups())
version = ".".join(str(part) for part in found)
if found < floor:
    emit("fail", f"shpool {version} is older than the required {minimum}; upgrade with: {fix}")
elif found > floor:
    emit(
        "warn",
        f"shpool {version} is newer than the tested {minimum}; pin the tested release with: {fix}",
    )
else:
    emit("ok", f"shpool {version} matches the tested release")
PY
}

# Report systemd-logind lingering for the current user. Without it logind
# stops the user manager at the last logout, which takes the shpool daemon and
# every managed session with it, so an install that looks complete still loses
# every session the first time the operator disconnects.
#
# Prints one "<status>\t<detail>" line; status is ok, warn, or fail.
logind_linger_report() {
  local user=${USER:-} state=
  [[ -n $user ]] || user=$(id -un 2>/dev/null) || user=
  if [[ -z $user ]]; then
    printf 'warn\t%s\n' \
      "the current user name could not be read, so lingering was not checked"
    return 0
  fi
  if ! command -v loginctl >/dev/null 2>&1; then
    printf 'warn\t%s\n' \
      "loginctl is unavailable, so lingering could not be checked; without it every managed session ends at logout"
    return 0
  fi
  state=$(loginctl show-user "$user" --property=Linger --value 2>/dev/null) ||
    state=
  state=${state#Linger=}
  case $state in
    yes)
      printf 'ok\t%s\n' \
        "logind lingering is enabled for $user, so managed sessions survive logout"
      ;;
    no)
      printf 'fail\t%s\n' \
        "logind lingering is disabled for $user, so every managed session ends at logout; enable it with: loginctl enable-linger $user"
      ;;
    *)
      printf 'warn\t%s\n' \
        "logind reported no lingering state for $user; check it with: loginctl show-user $user --property=Linger"
      ;;
  esac
}

validate_roots() {
  python3 - "$HOME" "$install_root" "$bin_dir" "$config_root" "$state_root" \
    "$service_root" "$bashrc_path" "$bash_profile_path" "$zshrc_path" \
    "${SESSION_KIT_TESTING:-0}" <<'PY'
import os
import pathlib
import sys

home, *raw, testing = sys.argv[1:]
home_path = pathlib.Path(home).resolve(strict=False)
labels = (
    "install root", "binary directory", "config root", "state root",
    "service root", "Bash file", "Bash login file", "zsh file",
)
paths = [pathlib.Path(value).resolve(strict=False) for value in raw]
raw_paths = [pathlib.Path(value) for value in raw]
for label, raw_path, path in zip(labels, raw_paths, paths):
    if not raw_path.is_absolute():
        raise SystemExit(f"session-kit: {label} must be absolute: {raw_path}")
    normalized = pathlib.Path(os.path.abspath(raw_path))
    if normalized != path:
        raise SystemExit(f"session-kit: refusing symlinked {label}: {raw_path}")
    if path == pathlib.Path("/") or path == home_path:
        raise SystemExit(f"session-kit: refusing broad {label}: {path}")
    if len(path.parts) < 4 and testing != "1":
        raise SystemExit(f"session-kit: refusing broad {label}: {path}")
install, binary, config, state, service, bashrc, bash_profile, zshrc = paths
for label, path in (("install root", install), ("config root", config), ("state root", state)):
    if path in {
        home_path / ".local",
        home_path / ".local" / "lib",
        home_path / ".local" / "state",
        home_path / ".config",
    }:
        raise SystemExit(f"session-kit: refusing broad {label}: {path}")
for shell in (bashrc, bash_profile, zshrc):
    if shell.parent == pathlib.Path("/") or shell == home_path:
        raise SystemExit(f"session-kit: refusing unsafe shell file: {shell}")
if len({bashrc, bash_profile, zshrc}) != 3:
    raise SystemExit("session-kit: shell integration files must differ")
if len({install, binary, config, state, service}) != 5:
    raise SystemExit("session-kit: lifecycle roots must not overlap exactly")
roots = (install, binary, config, state, service)
for index, left in enumerate(roots):
    for right in roots[index + 1:]:
        if left in right.parents or right in left.parents:
            raise SystemExit(f"session-kit: lifecycle roots must not contain one another: {left}, {right}")
PY
}

stat_mode() {
  stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1" 2>/dev/null
}

release_id_from_source() {
  local source=$1 release_id=${SESSION_KIT_RELEASE_ID:-}
  if [[ -n $release_id && ${SESSION_KIT_TESTING:-0} != 1 ]]; then
    die "SESSION_KIT_RELEASE_ID is reserved for isolated tests"
  fi
  if [[ -z $release_id ]] && source_is_git_root "$source"; then
    release_id=$(git -C "$source" rev-parse HEAD 2>/dev/null || true)
  elif [[ -z $release_id ]]; then
    release_id=$(source_record_commit "$source") ||
      die "install source needs an exact Git root or valid SOURCE.json artifact"
  fi
  [[ $release_id =~ ^[0-9a-f]{40}$ ]] ||
    die "install source must identify one full 40-character commit"
  printf '%s\n' "$release_id"
}

source_is_git_root() {
  local source=$1 top=
  command -v git >/dev/null 2>&1 || return 1
  top=$(git -C "$source" rev-parse --show-toplevel 2>/dev/null) || return 1
  [[ $(cd -- "$top" && pwd -P) == "$source" ]]
}

source_record_commit() {
  python3 - "$1/SOURCE.json" <<'PY'
import json
import os
import pathlib
import re
import stat
import sys

path = pathlib.Path(sys.argv[1])
try:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_size > 4096:
        raise ValueError("unsafe SOURCE.json")
    value = json.loads(path.read_text(encoding="utf-8"))
except (OSError, UnicodeError, ValueError):
    raise SystemExit(1)
if (
    not isinstance(value, dict)
    or value.get("schema_version") != 1
    or set(value) != {"schema_version", "source_commit", "exported_files"}
    or not re.fullmatch(r"[0-9a-f]{40}", str(value.get("source_commit", "")))
    or not isinstance(value.get("exported_files"), int)
    or isinstance(value.get("exported_files"), bool)
    or value["exported_files"] < 1
):
    raise SystemExit(1)
print(value["source_commit"])
PY
}

check_source() {
  local source=$1 failed=0 current_platform command required
  local -a missing_commands=() missing_files=()
  # Same renderer, same check names as `session-kit doctor`.
  check_row() {
    render_check_row "$1" "$2" "$3"
    [[ $1 != fail ]] || failed=1
  }
  current_platform=$(platform)
  if [[ $current_platform != unsupported ]]; then
    check_row ok platform "$current_platform"
  else
    check_row fail platform "unsupported operating system; Session Kit needs Linux or macOS"
  fi
  local -a required_commands=("${common_required_commands[@]}")
  if [[ $current_platform == linux ]]; then
    required_commands+=(flock journalctl sha256sum systemctl)
  elif [[ $current_platform == macos ]]; then
    required_commands+=(launchctl plutil sw_vers)
  fi
  for command in "${required_commands[@]}"; do
    command -v "$command" >/dev/null 2>&1 || missing_commands+=("$command")
  done
  if ((${#missing_commands[@]} == 0)); then
    check_row ok prerequisites "all required commands are available"
  else
    check_row fail prerequisites "commands unavailable: ${missing_commands[*]}"
  fi
  if (( BASH_VERSINFO[0] < 4 )); then
    check_row fail bash "Bash 4 or newer is required"
  else
    check_row ok bash "$BASH_VERSION"
  fi
  local python_floor=10 python_version=
  [[ $current_platform != macos ]] || python_floor=11
  python_version=$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null) ||
    python_version=
  if python3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, $python_floor) else 1)"; then
    check_row ok python "${python_version:-available}"
  else
    check_row fail python "Python 3.$python_floor or newer is required on $current_platform"
  fi
  local shpool_status= shpool_detail=
  IFS=$'\t' read -r shpool_status shpool_detail < <(shpool_version_report)
  check_row "$shpool_status" shpool-version "$shpool_detail"
  if [[ $current_platform == linux ]]; then
    if [[ ! -d /proc && ${SESSION_KIT_TESTING:-0} != 1 ]]; then
      check_row fail process "/proc is unavailable"
    else
      check_row ok process "Linux /proc is available"
    fi
    local manager_transport=
    if [[ ${SESSION_KIT_TESTING:-0} == 1 ]]; then
      check_row ok services "systemd user manager is available"
    elif manager_transport=$(systemd_user_manager_transport); then
      if [[ $manager_transport == direct ]]; then
        check_row ok services "systemd user manager is available"
      else
        check_row warn services "systemd user manager is available through local-machine transport; direct user socket is unavailable"
      fi
    else
      check_row fail services "systemd user manager is unavailable"
    fi
    # Lingering is a documented install step the installer cannot perform for
    # the operator, so preflight reports it without blocking the install. The
    # post-install doctor is the gate that fails on it.
    local linger_status= linger_detail=
    IFS=$'\t' read -r linger_status linger_detail < <(logind_linger_report)
    if [[ $linger_status == ok ]]; then
      check_row ok linger "$linger_detail"
    else
      check_row warn linger "$linger_detail"
    fi
  elif [[ $current_platform == macos ]]; then
    if [[ ${SESSION_KIT_TESTING:-0} != 1 ]]; then
      local mac_major mac_arch
      mac_major=$(sw_vers -productVersion | cut -d. -f1)
      mac_arch=$(uname -m)
      if [[ $mac_major =~ ^[0-9]+$ && $mac_major -ge 14 ]]; then
        check_row ok macos-version "macOS $mac_major"
      else
        check_row fail macos-version "macOS 14 or newer is required"
      fi
      if [[ $mac_arch == arm64 || $mac_arch == x86_64 ]]; then
        check_row ok architecture "$mac_arch"
      else
        check_row fail architecture "unsupported Mac architecture: $mac_arch"
      fi
    fi
    if [[ ${SESSION_KIT_TESTING:-0} == 1 ]] ||
        python3 "$source/lib/session_inventory.py" platform boot-id >/dev/null 2>&1; then
      check_row ok process "Darwin native process adapter is available"
    else
      check_row fail process "Darwin native process adapter is unavailable"
    fi
    check_row ok services "launchd is available"
  fi
  if command -v claude >/dev/null 2>&1 || command -v codex >/dev/null 2>&1; then
    check_row ok provider "Claude Code or Codex is available"
  else
    check_row fail provider "no provider is installed; install Claude Code, Codex, or both"
  fi
  # The theme layout applies its own stricter rule to the Codex home and runs
  # at the start of the install transaction. Reporting its refusal here, in its
  # own words, keeps it from stopping an install the preflight called
  # installable.
  local codex_detail=
  if codex_detail=$(codex_theme_layout validate 2>&1); then
    check_row ok codex-home "the theme layout is owner-controlled"
  else
    check_row fail codex-home "${codex_detail#session-kit: }"
  fi
  for required in \
    bin/sp bin/shpool_login bin/shpool_status bin/shpool_reaper \
    bin/codex_resume_here bin/session_kit_common bin/session-kit \
    "${inventory_files[@]}" "${shell_modules[@]}" bashrc/shpool.bashrc \
    deploy/session-kit-launcher config/projects.example.tsv \
    config/session-inventory.example.json config/shpool.example.toml; do
    [[ -f $source/$required ]] || missing_files+=("$required")
  done
  if ((${#missing_files[@]} == 0)); then
    check_row ok source-files "every required file is present"
  else
    for required in "${missing_files[@]}"; do
      check_row fail source-files "missing from the source: $required"
    done
  fi
  if source_is_git_root "$source"; then
    if [[ ${SESSION_KIT_TESTING:-0} != 1 ]] &&
      [[ -n $(git -C "$source" status --porcelain --untracked-files=all) ]]; then
      check_row fail provenance "the source worktree has uncommitted or untracked files"
    else
      check_row ok provenance "clean Git root"
    fi
  elif source_record_commit "$source" >/dev/null 2>&1; then
    check_row ok provenance "release artifact"
  else
    check_row fail provenance "the source has neither an exact Git root nor a valid SOURCE.json"
  fi
  (( failed == 0 )) || return 1
  validate_roots
  release_id_from_source "$source" >/dev/null
  check_row ok source "installable"
}

validate_purge_ownership() {
  local kind=$1 root=$2 marker=$3
  [[ -r $receipt_path && -f $receipt_path && ! -L $receipt_path ]] ||
    die "purge requires a valid Session Kit install receipt"
  [[ -r $marker && -f $marker && ! -L $marker ]] ||
    die "purge requires an ownership marker for $root"
  python3 - "$receipt_path" "$marker" "$kind" "$root" <<'PY'
import json
import pathlib
import sys

receipt_path, marker_path, kind, root_raw = sys.argv[1:]
root = pathlib.Path(root_raw).resolve(strict=False)
try:
    receipt = json.loads(pathlib.Path(receipt_path).read_text(encoding="utf-8"))
    marker = json.loads(pathlib.Path(marker_path).read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"session-kit: invalid purge ownership evidence: {exc}")
if receipt.get("schema_version") != 2:
    raise SystemExit("session-kit: purge requires a current ownership receipt; run update first")
roots = receipt.get("roots")
if not isinstance(roots, dict) or pathlib.Path(str(roots.get(kind, ""))).resolve(strict=False) != root:
    raise SystemExit(f"session-kit: receipt does not own {kind} root: {root}")
if (
    marker.get("schema_version") != 1
    or marker.get("owner") != "session-kit"
    or marker.get("kind") != kind
    or pathlib.Path(str(marker.get("root", ""))).resolve(strict=False) != root
):
    raise SystemExit(f"session-kit: ownership marker does not match {kind} root: {root}")
PY
}

validate_mutable_targets() {
  local target resolved
  while IFS= read -r target; do
    [[ ! -e $target && ! -L $target ]] && continue
    if [[ -L $target ]]; then
      [[ $target == "$install_root/current" ]] && continue
      # Pre-receipt layouts linked helpers into the install root itself. A
      # symlink resolving inside $install_root is the kit's own legacy
      # launcher; install(1) replaces the link, never its target.
      resolved=$(python3 - "$target" <<'PY'
import pathlib,sys
try:
    print(pathlib.Path(sys.argv[1]).resolve(strict=True))
except OSError:
    raise SystemExit(1)
PY
      ) || resolved=""
      [[ -n $resolved && $resolved == "$install_root"/* ]] ||
        die "refusing symlinked lifecycle target: $target"
      continue
    fi
    [[ -f $target ]] ||
      die "refusing incompatible lifecycle target: $target"
  done < <(transaction_targets)
}

validate_uninstall_launchers() {
  local release_id previous_release= helper target expected matched
  release_id=$(current_release_id)
  [[ $release_id =~ ^[0-9a-f]{40}$ ]] ||
    die "uninstall requires a valid active Session Kit release"
  verify_release "$install_root/releases/$release_id" "$release_id"
  if [[ -r $receipt_path ]]; then
    previous_release=$(python3 - "$receipt_path" <<'PY'
import json
import re
import sys
try:
    value = json.load(open(sys.argv[1], encoding="utf-8")).get("previous_release", "")
except (OSError, ValueError):
    value = ""
print(value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) else "")
PY
    )
  fi
  if [[ -n $previous_release ]]; then
    verify_release "$install_root/releases/$previous_release" "$previous_release"
  fi
  for helper in "${helpers[@]}"; do
    target=$bin_dir/$helper
    [[ ! -e $target && ! -L $target ]] && continue
    [[ -f $target && ! -L $target ]] ||
      die "refusing to remove unsafe launcher: $target"
    matched=0
    for expected in \
      "$install_root/releases/$release_id/deploy/session-kit-launcher" \
      ${previous_release:+"$install_root/releases/$previous_release/deploy/session-kit-launcher"}; do
      [[ -f $expected && ! -L $expected ]] && cmp -s -- "$target" "$expected" && matched=1
    done
    (( matched )) ||
      die "refusing to remove launcher with unowned content: $target"
  done
  target=$bin_dir/session-kit
  if [[ -e $target || -L $target ]]; then
    [[ -f $target && ! -L $target ]] ||
      die "refusing to remove unsafe command: $target"
    matched=0
    for expected in \
      "$install_root/releases/$release_id/deploy/session-kit-launcher" \
      ${previous_release:+"$install_root/releases/$previous_release/deploy/session-kit-launcher"}; do
      [[ -f $expected && ! -L $expected ]] && cmp -s -- "$target" "$expected" && matched=1
    done
    (( matched )) ||
      die "refusing to remove command with unowned content: $target"
  fi
}

verify_release() {
  python3 - "$1" "${2:-}" <<'PY'
import hashlib
import json
import pathlib
import re
import stat
import sys

root = pathlib.Path(sys.argv[1])
expected_commit = sys.argv[2]
if not root.is_dir() or root.is_symlink():
    raise SystemExit(f"session-kit: release is not a real directory: {root}")
try:
    meta = json.loads((root / "RELEASE.json").read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"session-kit: invalid release metadata: {exc}")
if (
    meta.get("schema_version") != 1
    or meta.get("lifecycle_schema_version") != 2
    or not isinstance(meta.get("commit"), str)
):
    raise SystemExit("session-kit: incompatible release metadata")
commit = meta["commit"]
if not re.fullmatch(r"[0-9a-f]{40}", commit) or root.name != commit:
    raise SystemExit("session-kit: invalid release identity")
if expected_commit and commit != expected_commit:
    raise SystemExit("session-kit: release identity does not match requested commit")
manifest = root / "MANIFEST.sha256"
try:
    lines = manifest.read_text(encoding="utf-8").splitlines()
except OSError as exc:
    raise SystemExit(f"session-kit: missing release manifest: {exc}")
expected = {}
for number, line in enumerate(lines, 1):
    match = re.fullmatch(r"([0-9a-f]{64})  ([^\0]+)", line)
    if not match:
        raise SystemExit(f"session-kit: malformed release manifest line {number}")
    rel = pathlib.PurePosixPath(match.group(2))
    if rel.is_absolute() or ".." in rel.parts or str(rel) in expected:
        raise SystemExit("session-kit: unsafe release manifest path")
    expected[str(rel)] = match.group(1)
actual = {}
for path in root.rglob("*"):
    rel = path.relative_to(root).as_posix()
    if path.is_symlink():
        raise SystemExit(f"session-kit: release contains symlink: {rel}")
    if path.is_file() and rel != "MANIFEST.sha256":
        actual[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    if path.is_dir() and stat.S_IMODE(path.stat().st_mode) & 0o222:
        raise SystemExit(f"session-kit: writable immutable release directory: {rel}")
if expected != actual:
    differing = sorted(set(expected) ^ set(actual))
    if not differing:
        differing = sorted(name for name in expected if expected[name] != actual[name])
    raise SystemExit(f"session-kit: release verification failed: {', '.join(differing[:3])}")
for rel in ("bin/session-kit", "deploy/session-kit-launcher"):
    path = root / rel
    if not path.is_file() or path.is_symlink() or not stat.S_IMODE(path.stat().st_mode) & 0o111:
        raise SystemExit(f"session-kit: release executable is invalid: {rel}")
PY
}
