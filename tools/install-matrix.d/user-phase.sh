#!/usr/bin/env bash
# The documented install, run as the unprivileged test user inside a matrix
# container. Every command here is one a reader of docs/install.md would type.
# Emits "MATRIX_RESULT <step> <exit-code>" lines that tools/install-matrix
# collects into the per-distro summary.
set -uo pipefail

export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

checkout=$HOME/session-kit
rc=0

step() {
  printf '\n===== STEP %s =====\n' "$1"
}

record() {
  printf 'MATRIX_RESULT %s %s\n' "$1" "$2"
}

# A step whose failure is a real install blocker.
run_gate() {
  local name=$1
  shift
  step "$name"
  printf '$ %s\n' "$*"
  "$@"
  local status=$?
  record "$name" "$status"
  if ((status != 0)); then
    rc=1
  fi
  return 0
}

# A step recorded for evidence whose exit code is not a pass/fail gate (for
# example the pre-activation doctor, which is documented to fail while the
# user units are installed and not yet enabled).
run_note() {
  local name=$1
  shift
  step "$name"
  printf '$ %s\n' "$*"
  "$@"
  record "$name" "$?"
  return 0
}

step environment
id
printf 'bash %s\n' "$BASH_VERSION"
python3 --version
uname -srm
sed -n '1,3p' /etc/os-release
printf 'XDG_RUNTIME_DIR=%s\n' "$XDG_RUNTIME_DIR"

# A real machine reaches this point with a login session; the container gets
# the same state from lingering, enabled by root before this script runs.
run_gate user-manager systemctl --user show-environment

# Clone the source under test from the local repository, so the run never
# depends on GitHub being reachable. A clone is a clean Git root, which is what
# the installer's source-provenance check requires.
run_gate clone git clone --no-hardlinks --quiet /src "$checkout"
if [[ -d $checkout/.git ]]; then
  printf 'source commit: %s\n' "$(git -C "$checkout" rev-parse HEAD)"
  printf 'source branch: %s\n' "$(git -C "$checkout" rev-parse --abbrev-ref HEAD)"
fi

# shpool is not packaged by any distribution. The static binary at /shpool was
# produced by the container build route in extras/build-static-binary.md and is
# installed exactly where that document puts it.
step shpool-install
mkdir -p "$HOME/.cargo/bin"
install -m 755 /shpool/shpool "$HOME/.cargo/bin/shpool"
shpool version
record shpool-install "$?"

cd "$checkout" || exit 1

run_gate check ./install.sh --check
run_gate install ./install.sh --non-interactive
run_note doctor-before-activation session-kit doctor
run_gate enable-login session-kit enable-login
run_gate services-enable session-kit services enable
run_note services-status session-kit services status
run_gate doctor session-kit doctor

step doctor-json
session-kit doctor --json >"$HOME/doctor.json"
record doctor-json "$?"
cat "$HOME/doctor.json"

step unit-state
systemctl --user list-units --no-pager --all \
  'shpool*' 'session-kit*' 2>&1 || true
loginctl show-user "$(id -un)" --property=Linger 2>&1 || true

step summary
printf 'MATRIX_OVERALL %s\n' "$rc"
exit "$rc"
