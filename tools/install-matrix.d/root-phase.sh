#!/usr/bin/env bash
# Runs as root inside a matrix container after systemd has started. It creates
# only the state a real machine already has - a lingering login session for the
# operator account - and then hands the documented install to that account.
set -uo pipefail

user=${MATRIX_USER:-tester}
uid=$(id -u "$user") || exit 1

printf '===== STEP container-systemd =====\n'
systemctl is-system-running || true
systemctl --version | head -1

# docs/install.md: "Enable this before session-kit services enable, not after."
printf '\n===== STEP linger =====\n'
loginctl enable-linger "$user"
for _ in 1 2 3 4 5 6 7 8 9 10; do
  [[ -d /run/user/$uid ]] && break
  sleep 1
done
loginctl show-user "$user" --property=Linger
printf 'MATRIX_RESULT linger %s\n' "$?"

# su keeps the environment minimal, which is what a fresh SSH login gives.
runuser -u "$user" -- env \
  HOME="/home/$user" \
  XDG_RUNTIME_DIR="/run/user/$uid" \
  TERM=dumb \
  bash /matrix/user-phase.sh
exit "$?"
