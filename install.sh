#!/usr/bin/env bash
set -euo pipefail

if [[ $(uname -s 2>/dev/null) == Darwin && ${BASH_VERSINFO[0]} -lt 4 ]]; then
  for modern_bash in /opt/homebrew/bin/bash /usr/local/bin/bash; do
    if [[ -x $modern_bash ]]; then
      export PATH="${modern_bash%/*}:$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
      exec "$modern_bash" "$0" "$@"
    fi
  done
  printf '%s\n' 'session-kit: macOS needs Homebrew Bash 4 or newer. Install it with: brew install bash python rust' >&2
  exit 69
fi

repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)

case "${1:-}" in
  --check)
    shift
    exec "$repo_dir/bin/session-kit" check --source "$repo_dir" "$@"
    ;;
  --help|-h)
    exec "$repo_dir/bin/session-kit" help
    ;;
  *)
    exec "$repo_dir/bin/session-kit" install --source "$repo_dir" "$@"
    ;;
esac
