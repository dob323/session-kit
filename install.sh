#!/usr/bin/env bash
set -euo pipefail

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
