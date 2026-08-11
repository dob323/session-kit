#!/usr/bin/env bash
# Picker projects menu: list, add, review discovered directories, and drop.
# Source this file; do not execute it.
#
# Source order: bin/shpool_login sources this module ahead of its first trap.
# These functions read PROJECTS_TOOL and SK_PROJECTS_FILE, resolved in that
# file and in bin/session_kit_common respectively, and call picker_read() and
# picker_clear_screen() from the live and theme modules.
#
# Globals the entry script owns are assigned there, not here.
# shellcheck disable=SC2154

# ---- projects ----------------------------------------------------------
# Adding a project used to mean knowing an alias, a provider keyword, and an
# absolute path — none of which the picker ever offered. It lives here rather
# than on the main screen because you add a project a few times a year.
projects_tool() {
  [[ -f $PROJECTS_TOOL ]] || return 1
  python3 "$PROJECTS_TOOL" "$@"
}

show_projects_menu() {
  local snapshot answer
  while true; do
    new_temp login-projects-state || return 0
    snapshot=$NEW_TEMP
    if ! projects_tool --json candidates > "$snapshot" 2>/dev/null; then
      echo "  The project list is unavailable."
      return 0
    fi
    echo
    python3 - "$snapshot" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
configured = [row for row in data.get("configured", []) if row.get("provider") != "ignore"]
candidates = data.get("candidates", [])
ignored = data.get("ignored", [])
print("  Projects")
if configured:
    for number, row in enumerate(configured, 1):
        print(f"  {number:2}  {row['alias']}: {row['cwd']}")
else:
    print("     None yet.")
if candidates:
    print(
        f"     {len(candidates)} directory(s) your providers use "
        "are not on this list."
    )
if ignored:
    print(f"     {len(ignored)} kept off it for good.")
PY
    echo
    if python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))["candidates"] else 1)' \
        "$snapshot" 2>/dev/null; then
      echo "  a  Add a directory · i  Review the ones not listed · d  Drop number · Enter  Back"
    else
      echo "  a  Add a directory · d  Drop number · Enter  Back"
    fi
    echo
    picker_modal_read answer "  projects ❯ " || return 0
    case "$answer" in
      "") return 0 ;;
      a|A) projects_add ;;
      i|I) projects_review "$snapshot" ;;
      d|D) echo "  Use d followed by a number, such as d 2." ;;
      d\ *|D\ *) projects_drop "${answer#* }" "$snapshot" ;;
      *) echo "  Unknown choice. Use a, i, d number, or Enter." ;;
    esac
  done
}

projects_add() {
  local directory alias provider suggestion
  echo
  echo "  Add a project"
  picker_modal_read directory "  Directory (Enter for $PWD) ❯ " || return 0
  [[ -n $directory ]] || directory=$PWD
  if [[ $directory != /* ]]; then
    echo "  Use an absolute path. Nothing added."
    return 0
  fi
  if [[ ! -d $directory ]]; then
    echo "  That directory does not exist. Nothing added."
    return 0
  fi
  suggestion=$(projects_tool --json suggest "$directory" 2>/dev/null |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["alias"])' 2>/dev/null) ||
    suggestion=""
  if [[ -n $suggestion ]]; then
    picker_modal_read alias "  Short name [$suggestion] ❯ " || return 0
    [[ -n $alias ]] || alias=$suggestion
  else
    picker_modal_read alias "  Short name ❯ " || return 0
    [[ -n $alias ]] || { echo "  A short name is required. Nothing added."; return 0; }
  fi
  picker_modal_read provider "  Opens as [claude] ❯ " || return 0
  [[ -n $provider ]] || provider=claude
  case "$provider" in
    claude|codex|shell) ;;
    *) echo "  Choose claude, codex, or shell. Nothing added."; return 0 ;;
  esac
  if projects_tool add "$alias" "$provider" "$directory"; then
    sk_log_action projects_add added || true
  fi
}

projects_review() {
  local snapshot=$1 answer chosen
  echo
  python3 - "$snapshot" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    candidates = json.load(handle).get("candidates", [])
print("  Not on your list")
for number, row in enumerate(candidates, 1):
    print(f"  {number:2}  [{row['provider'].title():6}] {row['cwd']}  →  {row['alias']}")
PY
  echo
  echo "  numbers/ranges: add those · a: add all · x: never list them · Enter: Back"
  echo
  picker_modal_read answer "  review ❯ " || return 0
  [[ -n $answer ]] || return 0
  if [[ ${answer,,} != x ]]; then
    projects_tool import --select "$answer" || true
    return 0
  fi
  # x means all of them, permanently.
  chosen=$(projects_tool candidates --select a 2>/dev/null) || {
    echo "  The list is unavailable. Nothing changed."
    return 0
  }
  local -a directories=()
  mapfile -t directories <<<"$chosen"
  local directory count=0
  for directory in "${directories[@]}"; do
    [[ -n $directory ]] || continue
    projects_tool ignore "$directory" >/dev/null || continue
    count=$(( count + 1 ))
  done
  echo "  Kept $count directory(s) off the list for good."
}

projects_drop() {
  local number=$1 snapshot=$2 directory
  [[ $number =~ ^[0-9]+$ ]] || {
    echo "  Use d followed by a number, such as d 2."
    return 0
  }
  directory=$(python3 - "$snapshot" "$number" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
rows = [row for row in data.get("configured", []) if row.get("provider") != "ignore"]
index = int(sys.argv[2])
if index < 1 or index > len(rows):
    raise SystemExit(2)
print(rows[index - 1]["cwd"])
PY
) || {
    echo "  Choose a number shown here. Nothing changed."
    return 0
  }
  projects_tool ignore "$directory" || true
}
