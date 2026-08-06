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
  current_platform=$(platform)
  case "$current_platform" in
    linux) printf 'OK    operating system: Linux\n' ;;
    macos) printf 'OK    operating system: macOS\n' ;;
    *)
      printf 'FAIL  operating system: only Linux and macOS are supported\n'
      failed=1
      ;;
  esac
  local -a required_commands=("${common_required_commands[@]}")
  if [[ $current_platform == linux ]]; then
    required_commands+=(flock journalctl systemctl)
  elif [[ $current_platform == macos ]]; then
    required_commands+=(launchctl plutil sw_vers)
  fi
  for command in "${required_commands[@]}"; do
    if command -v "$command" >/dev/null 2>&1; then
      printf 'OK    command: %s\n' "$command"
    else
      printf 'FAIL  command missing: %s\n' "$command"
      failed=1
    fi
  done
  if (( BASH_VERSINFO[0] < 4 )); then
    printf 'FAIL  Bash 4 or newer is required\n'
    failed=1
  else
    printf 'OK    Bash: %s\n' "$BASH_VERSION"
  fi
  local python_floor=10
  [[ $current_platform != macos ]] || python_floor=11
  if ! python3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, $python_floor) else 1)"; then
    printf 'FAIL  Python 3.%s or newer is required on %s\n' "$python_floor" "$current_platform"
    failed=1
  fi
  if ! command -v shpool >/dev/null 2>&1; then
    printf 'FAIL  shpool missing: install v0.11.0 with cargo install shpool --version 0.11.0 --locked\n'
    failed=1
  fi
  if [[ $current_platform == linux ]]; then
    if [[ ! -d /proc && ${SESSION_KIT_TESTING:-0} != 1 ]]; then
      printf 'FAIL  Linux /proc is required for process identity checks\n'
      failed=1
    else
      printf 'OK    process adapter: Linux /proc\n'
    fi
    local manager_transport=
    if [[ ${SESSION_KIT_TESTING:-0} == 1 ]]; then
      printf 'OK    user service manager: systemd\n'
    elif manager_transport=$(systemd_user_manager_transport); then
      if [[ $manager_transport == direct ]]; then
        printf 'OK    user service manager: systemd\n'
      else
        printf 'WARN  user service manager: available through local-machine transport; direct user socket is unavailable\n'
      fi
    else
      printf 'FAIL  systemd user manager is unavailable\n'
      failed=1
    fi
  elif [[ $current_platform == macos ]]; then
    if [[ ${SESSION_KIT_TESTING:-0} != 1 ]]; then
      local mac_major mac_arch
      mac_major=$(sw_vers -productVersion | cut -d. -f1)
      mac_arch=$(uname -m)
      [[ $mac_major =~ ^[0-9]+$ && $mac_major -ge 14 ]] || {
        printf 'FAIL  macOS 14 or newer is required\n'
        failed=1
      }
      [[ $mac_arch == arm64 || $mac_arch == x86_64 ]] || {
        printf 'FAIL  unsupported Mac architecture: %s\n' "$mac_arch"
        failed=1
      }
    fi
    if [[ ${SESSION_KIT_TESTING:-0} == 1 ]] ||
        python3 "$source/lib/session_inventory.py" platform boot-id >/dev/null 2>&1; then
      printf 'OK    process adapter: Darwin native APIs\n'
    else
      printf 'FAIL  Darwin native process or boot APIs are unavailable\n'
      failed=1
    fi
    printf 'OK    user service manager: launchd\n'
  fi
  if command -v claude >/dev/null 2>&1 || command -v codex >/dev/null 2>&1; then
    printf 'OK    provider CLI: Claude Code or Codex\n'
  else
    printf 'FAIL  install Claude Code, Codex, or both\n'
    failed=1
  fi
  for required in \
    bin/sp bin/shpool_login bin/shpool_status bin/shpool_reaper \
    bin/codex_resume_here bin/session_kit_common bin/session-kit \
    "${inventory_files[@]}" "${shell_modules[@]}" bashrc/shpool.bashrc \
    deploy/session-kit-launcher config/projects.example.tsv \
    config/session-inventory.example.json config/shpool.example.toml; do
    if [[ ! -f $source/$required ]]; then
      printf 'FAIL  source file missing: %s\n' "$required"
      failed=1
    fi
  done
  if source_is_git_root "$source"; then
    if ! command -v git >/dev/null 2>&1; then
      printf 'FAIL  command missing: git (required for Git checkout installs)\n'
      failed=1
    fi
    if [[ ${SESSION_KIT_TESTING:-0} != 1 ]] &&
      [[ -n $(git -C "$source" status --porcelain --untracked-files=all) ]]; then
      printf 'FAIL  source worktree has uncommitted or untracked files\n'
      failed=1
    else
      printf 'OK    source provenance: clean Git root\n'
    fi
  elif source_record_commit "$source" >/dev/null 2>&1; then
    printf 'OK    source provenance: release artifact\n'
  else
    printf 'FAIL  source has neither an exact Git root nor valid SOURCE.json\n'
    failed=1
  fi
  (( failed == 0 )) || return 1
  validate_roots
  release_id_from_source "$source" >/dev/null
  printf 'OK    source: installable\n'
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
  local release_id helper target expected
  release_id=$(current_release_id)
  [[ $release_id =~ ^[0-9a-f]{40}$ ]] ||
    die "uninstall requires a valid active Session Kit release"
  verify_release "$install_root/releases/$release_id" "$release_id"
  for helper in "${helpers[@]}"; do
    target=$bin_dir/$helper
    expected=$install_root/releases/$release_id/deploy/session-kit-launcher
    [[ ! -e $target && ! -L $target ]] && continue
    [[ -f $target && ! -L $target ]] ||
      die "refusing to remove unsafe launcher: $target"
    cmp -s -- "$target" "$expected" ||
      die "refusing to remove launcher with unowned content: $target"
  done
  target=$bin_dir/session-kit
  expected=$install_root/releases/$release_id/bin/session-kit
  if [[ -e $target || -L $target ]]; then
    [[ -f $target && ! -L $target ]] ||
      die "refusing to remove unsafe command: $target"
    cmp -s -- "$target" "$expected" ||
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
