#!/usr/bin/env bash
# Session Kit project discovery and first-install import prompt.
# Source this file; do not execute it.
#
# Source order: bin/session-kit sources this module after it assigns
# install_root, config_root, state_root, and projects_created, and after it
# defines die(). install_defaults() sets projects_created before
# configure_initial_projects() reads it.
#
# Globals the entry script owns are assigned there, not here.
# shellcheck disable=SC2154

projects_tool() {
  local release=${1:-$install_root/current}
  shift || true
  local tool=$release/lib/sessionkit_inventory/projects.py
  [[ -f $tool && ! -L $tool ]] || die "project discovery tool is unavailable"
  python3 "$tool" --projects-file "$config_root/projects.tsv" \
    --state-dir "$state_root" "$@"
}

configure_initial_projects() {
  local choice=$1 noninteractive=$2 answer count discovery
  (( projects_created )) || return 0
  if [[ $choice == import ]]; then
    projects_tool "$install_root/current" import
    return
  fi
  if [[ $choice == skip || $noninteractive == 1 || ! -t 0 ]]; then
    printf 'Project import skipped. Run session-kit projects import whenever you are ready.\n'
    return
  fi
  discovery=$(mktemp "$state_root/project-discovery.XXXXXX")
  if ! projects_tool "$install_root/current" --json candidates > "$discovery"; then
    command rm -f -- "$discovery"
    printf 'Project discovery was unavailable. Run session-kit projects import later.\n' >&2
    return
  fi
  count=$(python3 - "$discovery" <<'PY'
import json,sys
value=json.load(open(sys.argv[1],encoding="utf-8"))
print(len(value.get("candidates",[])))
for warning in value.get("warnings",[]):
    print(f"Warning: {warning}",file=sys.stderr)
PY
)
  if (( count == 0 )); then
    command rm -f -- "$discovery"
    printf 'No existing Claude Code or Codex project folders were found.\n'
    printf 'Add one later with session-kit projects add.\n'
    return
  fi
  # Never all-or-nothing. Every directory a provider has ever run in is
  # offered by number, with the short name it would get, and only the chosen
  # ones are written.
  python3 - "$discovery" <<'PY'
import json,sys
value=json.load(open(sys.argv[1],encoding="utf-8"))
rows=value["candidates"]
print(f"\nFound {len(rows)} directory(s) your providers have used:")
for number,row in enumerate(rows,1):
    print(f"  {number:2}  [{row['provider'].capitalize():6}] {row['cwd']}  →  {row['alias']}")
PY
  command rm -f -- "$discovery"
  printf '\nWhich should the picker offer? numbers/ranges, a for all, Enter for none: '
  read -r answer
  if [[ -z $answer ]]; then
    printf 'None added. Run session-kit projects import whenever you are ready.\n'
    return
  fi
  projects_tool "$install_root/current" import --select "$answer" || {
    printf 'Nothing added. Run session-kit projects import later.\n' >&2
    return
  }
}
