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
# absolute path, none of which the picker ever offered. It lives here rather
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
import unicodedata

def cells(text):
    return sum(
        0 if unicodedata.combining(character)
        else 2 if unicodedata.east_asian_width(character) in {"W", "F"}
        else 1
        for character in text
    )

def pad(text, width):
    return text + " " * max(0, width - cells(text))

with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
configured = [row for row in data.get("configured", []) if row.get("provider") != "ignore"]
candidates = data.get("candidates", [])
ignored = data.get("ignored", [])
print("  Projects")
if configured:
    alias_width = max(cells(row["alias"]) for row in configured)
    for number, row in enumerate(configured, 1):
        print(f"  {number:2}  {pad(row['alias'] + ':', alias_width + 1)} {row['cwd']}")
else:
    print("     None yet.")
if candidates:
    print(
        f"     {len(candidates)} directory(s) your providers use "
        "are not on this list."
    )
if ignored:
    print(f"     {len(ignored)} hidden from it, and can be restored.")
PY
    echo
    # "Drop" read like removing a line from a list. It hides the directory for
    # good, so the footer says that, and the key that takes it back is offered
    # on the same line whenever there is something to take back.
    local footer="  add a" has_candidates=0 has_ignored=0
    if python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))["candidates"] else 1)' \
        "$snapshot" 2>/dev/null; then
      has_candidates=1
      footer+=" · review the rest i"
    fi
    footer+=" · hide d number"
    if python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))["ignored"] else 1)' \
        "$snapshot" 2>/dev/null; then
      has_ignored=1
      footer+=" · restore u"
    fi
    echo "$footer · ↵ back · b back"
    echo
    picker_modal_read answer "  projects ❯ " || return 0
    case "$answer" in
      ""|b|B|back|q|Q) return 0 ;;
      a|A) projects_add ;;
      i|I)
        if (( has_candidates )); then
          projects_review "$snapshot"
        else
          echo "  Projects not on your list: none."
        fi
        ;;
      u|U)
        if (( has_ignored )); then
          projects_hidden "$snapshot"
        else
          echo "  Hidden projects: none."
        fi
        ;;
      d|D) echo "  That needs a project number, such as d 2. Numbers shown here work." ;;
      d\ *|D\ *) projects_drop "${answer#* }" "$snapshot" ;;
      d[0-9]*|D[0-9]*) projects_drop "${answer#[dD]}" "$snapshot" ;;
      *) picker_unknown_choice "a, i, u, d number, b, and Enter" ;;
    esac
  done
}

projects_add() {
  local directory alias suggestion
  echo
  echo "  Add a project"
  echo "  Claude, Codex, or a shell is chosen when you start a session in it."
  picker_modal_read directory "  Directory (Enter for $PWD) ❯ " || return 0
  [[ -n $directory ]] || directory=$PWD
  if [[ $directory != /* ]]; then
    echo "  That is not an absolute path. Nothing changed."
    return 0
  fi
  if [[ ! -d $directory ]]; then
    echo "  That directory does not exist. Nothing changed."
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
    [[ -n $alias ]] || { echo "  A project needs a short name. Nothing changed."; return 0; }
  fi
  # There is no third question here any more. A project is a directory; which
  # provider opens it is answered when a session starts, and the new-session
  # flow asks that first. Answering it here as well is what forced a directory
  # worked on with both providers onto two rows under two short names.
  if projects_tool add "$alias" "$directory"; then
    sk_log_action projects_add added || true
  fi
}

projects_review() {
  local snapshot=$1 answer chosen
  echo
  python3 - "$snapshot" <<'PY'
import json
import sys
import unicodedata

def cells(text):
    return sum(
        0 if unicodedata.combining(character)
        else 2 if unicodedata.east_asian_width(character) in {"W", "F"}
        else 1
        for character in text
    )

def pad(text, width):
    return text + " " * max(0, width - cells(text))

with open(sys.argv[1], encoding="utf-8") as handle:
    candidates = json.load(handle).get("candidates", [])
print("  Not on your list")
width = max((cells(row["cwd"]) for row in candidates), default=1)
for number, row in enumerate(candidates, 1):
    print(
        f"  {number:2}  [{row['provider'].title():6}] "
        f"{pad(row['cwd'], width)}  →  {row['alias']}"
    )
PY
  echo
  echo "  add numbers · add all a · never list x · ↵ back · b back"
  echo
  picker_modal_read answer "  review ❯ " || return 0
  [[ -n $answer ]] || return 0
  case "${answer,,}" in
    b|back) return 0 ;;
  esac
  if [[ ${answer,,} != x ]]; then
    projects_tool import --select "$answer" || true
    return 0
  fi
  # x means all of them, permanently. One keystroke used to be enough.
  chosen=$(projects_tool candidates --select a 2>/dev/null) || {
    echo "  The project list is unavailable. Nothing changed."
    return 0
  }
  local -a directories=()
  mapfile -t directories <<<"$chosen"
  local directory count=0 offered=0
  for directory in "${directories[@]}"; do
    [[ -n $directory ]] || continue
    offered=$(( offered + 1 ))
  done
  (( offered > 0 )) || {
    echo "  There is nothing left to hide. Nothing changed."
    return 0
  }
  for directory in "${directories[@]}"; do
    [[ -n $directory ]] || continue
    projects_tool ignore "$directory" >/dev/null || continue
    count=$(( count + 1 ))
  done
  printf '  Hid %s project%s. Restore them from Projects.\n' \
    "$count" "$([[ $count == 1 ]] || printf s)"
}

projects_drop() {
  local number=$1 snapshot=$2 directory
  [[ $number =~ ^[0-9]+$ ]] || {
    echo "  That needs a project number, such as d 2. Numbers shown here work."
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
    echo "  There is no such project on this screen. Numbers shown here work."
    return 0
  }
  # This never was the "drop a line" it sounded like: the directory leaves the
  # list until it is restored from this same menu, which the line below says.
  projects_tool ignore "$directory" || true
  printf '  Hid %s. Restore it from Projects.\n' "$directory"
}

# An ignore is a decision, not a deletion. This is where it is taken back:
# the directory becomes discoverable again and can be added like any other.
projects_hidden() {
  local snapshot=$1 answer directory
  echo
  python3 - "$snapshot" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    ignored = json.load(handle).get("ignored", [])
print("  Hidden directories")
for number, cwd in enumerate(ignored, 1):
    print(f"  {number:2}  {cwd}")
PY
  echo
  echo "  restore number · ↵ back · b back"
  echo
  picker_modal_read answer "  restore ❯ " || return 0
  [[ -n $answer ]] || return 0
  case "${answer,,}" in
    b|back) return 0 ;;
  esac
  [[ $answer =~ ^[0-9]+$ ]] || {
    echo "  There is no such project on this screen. Numbers shown here work."
    return 0
  }
  directory=$(python3 - "$snapshot" "$answer" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    ignored = json.load(handle).get("ignored", [])
index = int(sys.argv[2])
if index < 1 or index > len(ignored):
    raise SystemExit(2)
print(ignored[index - 1])
PY
) || {
    echo "  There is no such project on this screen. Numbers shown here work."
    return 0
  }
  if projects_tool unignore "$directory"; then
    sk_log_action projects_restore restored || true
  fi
}
