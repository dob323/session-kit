#!/usr/bin/env bash
# Picker action menu and the proof-bound actions behind it: open, take over,
# close, rename, fork, and guided new sessions. Every state change goes out as
# a proof-bound sp command. Source this file; do not execute it.
#
# Source order: bin/shpool_login sources this module ahead of its first trap.
# These functions read SNAPSHOT, VIEW, PAGE, QUERY, RECOVERY, SP_CMD,
# CONFIRM_FORGIVE, PICKER_REFUSED_STATUS, and PICKER_ATTACH_FAILED_STATUS, all
# assigned in that file before the boot sequence runs, and call into the theme,
# live, view, and render modules.
#
# Globals the entry script owns are assigned there, not here.
# shellcheck disable=SC2154

show_other_provider_sessions() {
  python3 - "$VIEW" <<'PY'
import json
import os
import re
import shutil
import sys
import unicodedata

with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
rows = data.get("outside_agents", [])
width = max(1, min(239, shutil.get_terminal_size(fallback=(100, 24)).columns - 1))

def clean(value):
    text = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in str(value or "")
    )
    return " ".join(text.split())

def cells(text):
    return sum(
        0 if unicodedata.combining(character)
        else 2 if unicodedata.east_asian_width(character) in {"W", "F"}
        else 1
        for character in text
    )

def shorten(text, room):
    text = clean(text)
    if cells(text) <= room:
        return text
    kept = []
    used = 0
    for character in text:
        size = cells(character)
        if used + size > max(0, room - 1):
            break
        kept.append(character)
        used += size
    return "".join(kept) + "…"

def project_hint(value):
    path = clean(value)
    if not path.startswith("/"):
        return ""
    parts = [part for part in path.split("/") if part]
    account_names = {
        parts[index + 1].casefold()
        for index, part in enumerate(parts[:-1])
        if part.casefold() in {"home", "users"}
    }
    account_names.add(os.path.basename(os.path.expanduser("~")).casefold())
    generic = {
        "app", "apps", "code", "current", "home", "repo", "repos", "src",
        "srv", "users", "v2", "var", "workspace", "workspaces",
    }
    for part in reversed(parts):
        folded = part.casefold()
        if (
            part
            and not part.startswith(".")
            and folded not in generic
            and folded not in account_names
        ):
            return part
    return ""

def private_names(value):
    path = clean(value)
    parts = [part for part in path.split("/") if part] if path.startswith("/") else []
    names = {
        parts[index + 1]
        for index, part in enumerate(parts[:-1])
        if part.casefold() in {"home", "users"}
    }
    home_name = os.path.basename(os.path.expanduser("~"))
    if home_name:
        names.add(home_name)
    return {name for name in names if name}

def safe_title(value, cwd, fallback):
    title = clean(value)
    path = clean(cwd)
    hint = project_hint(cwd)
    if path:
        title = title.replace(path, hint or fallback)
    for name in sorted(private_names(cwd), key=len, reverse=True):
        title = re.sub(re.escape(name), "account", title, flags=re.IGNORECASE)
    return clean(title) or fallback

print()
print("  Other provider sessions")
if data.get("stale"):
    print("  Last-known provider roots outside the session manager; they are not confirmed live or attachable here.")
else:
    print("  Detected live provider roots outside the session manager; they are not attachable here.")
if not rows:
    print("  No matching other provider sessions.")
else:
    previous_provider = None
    labels = {"claude": "Claude", "codex": "Codex"}
    for row in rows:
        provider = labels.get(
            str(row.get("display_provider") or row.get("provider") or "").casefold(),
            "Unknown",
        )
        if provider != previous_provider:
            print(f"    {provider}")
            previous_provider = provider
        hint = project_hint(row.get("cwd"))
        title = safe_title(
            row.get("display_title") or row.get("title"),
            row.get("cwd"),
            provider,
        )
        detail = "not attachable here"
        if hint:
            detail = f"project: {hint} | {detail}"
        prefix = "      -  "
        title_room = max(1, width - cells(prefix) - cells(detail) - 3)
        print(prefix + shorten(title, title_room) + " | " + detail)
print()
PY
  local ignored
  picker_modal_read ignored "  Enter: Back ❯ " || return 0
}

number_metadata() {
  local number=$1
  python3 - "$VIEW" "$number" <<'PY'
import base64
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
wanted = int(sys.argv[2])
matches = [
    row
    for row in data.get("sessions", [])
    if row.get("terminal_number") == wanted
]
if len(matches) != 1:
    raise SystemExit(2)
row = matches[0]
values = (
    row.get("availability"),
    "true" if row.get("mutation_allowed") is True else "false",
    row.get("display_shpool_id"),
    row.get("provider"),
    row.get("title"),
    row.get("shpool_id_raw"),
    (row.get("identity") or {}).get("uuid"),
    (row.get("identity") or {}).get("confidence"),
)
for value in values:
    print(base64.b64encode(str(value or "").encode()).decode())
PY
}

decode64() {
  printf '%s' "$1" | base64 -d
}

prompt_quarantine_selection() {
  local wanted=$1 token key state
  [[ $wanted =~ ^[0-9]+$ ]] || return 1
  while IFS=$'\t' read -r token key state; do
    if [[ $token == "q$wanted" && $key =~ ^[0-9a-f]{12}$ &&
          ( $state == intake_pending || $state == outcome_unknown ) ]]; then
      printf '%s\n%s\n' "$key" "$state"
      return 0
    fi
  done <<<"$PROMPT_QUARANTINE_INDEX"
  return 1
}

run_prompt_quarantine_action() {
  local action=$1
  shift
  local output status=0
  new_temp prompt-quarantine-action || {
    echo "  The prompt action could not be prepared. Nothing changed."
    return 1
  }
  output=$NEW_TEMP
  "$SP_CMD" prompt-quarantine "$action" "$@" >"$output" 2>&1 || status=$?
  if (( status != 0 )); then
    echo "  The prompt action was refused; its durable record was left unchanged."
    return "$status"
  fi
  case "$action" in
    ingest) echo "  The prompt was ingested without sending it to Codex again." ;;
    resume) echo "  The exact Codex conversation was resumed in a managed session." ;;
    discard) echo "  The prompt left Needs You and remains recoverable for 30 days." ;;
    prune) echo "  Expired prompt recovery records were pruned." ;;
  esac
}

prompt_quarantine_prune() {
  run_prompt_quarantine_action prune || true
}

dismiss_repair_failure() {
  local wanted=$1 token index="" answer
  [[ $wanted =~ ^[0-9]+$ ]] || return 1
  while IFS=$'\t' read -r token index; do
    [[ $token == "d$wanted" && $index =~ ^[0-9]+$ ]] && break
    index=""
  done <<<"$REPAIR_INDEX"
  if [[ -z $index ]]; then
    echo "  Choose a d-number shown here. Nothing changed."
    return 0
  fi
  picker_modal_read answer "  Type dismiss to confirm this unresolved repair is understood ❯ " || return 0
  if [[ ${answer,,} != dismiss ]]; then
    echo "  Repair failure kept. Nothing changed."
    return 0
  fi
  if python3 - "$REPAIR_FILE" "$index" <<'PY'
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

path = Path(sys.argv[1])
index = int(sys.argv[2])
flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
descriptor = os.open(path, flags)
with os.fdopen(descriptor, encoding="utf-8") as handle:
    metadata = os.fstat(handle.fileno())
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise SystemExit(2)
    data = json.load(handle)
entries = data.get("repairs") if isinstance(data, dict) else None
if not isinstance(entries, list) or not 0 <= index < len(entries):
    raise SystemExit(2)
item = entries[index]
if not isinstance(item, dict) or item.get("acknowledged") or item.get("outcome") == "repaired":
    raise SystemExit(2)
item["acknowledged"] = True
out, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
try:
    os.fchmod(out, 0o600)
    with os.fdopen(out, "w", encoding="utf-8") as handle:
        out = -1
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
finally:
    if out >= 0:
        os.close(out)
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
PY
  then
    echo "  Repair failure dismissed; its history remains in the repair log."
    CONFIRM_FORGIVE=1
  else
    echo "  The repair record changed or was unsafe to update. Nothing changed."
  fi
}

choose_prompt_quarantine() {
  local number=$1 metadata key state
  metadata=$(prompt_quarantine_selection "$number") || {
    echo "  Choose a q-number shown here. Nothing changed."
    return
  }
  key=${metadata%%$'\n'*}
  state=${metadata#*$'\n'}
  while true; do
    echo
    if [[ $state == intake_pending ]]; then
      echo "  Codex prompt intake pending"
    else
      echo "  Codex prompt outcome unknown"
    fi
    echo "  1  Ingest the accepted prompt without sending it to Codex again"
    echo "  2  Resume the exact Codex conversation in a managed session"
    echo "  3  Discard from Needs You (recoverable for 30 days)"
    echo "  4  Prune all prompt records older than 30 days"
    echo "  Enter  Back"
    echo
    local action answer resume_dir
    picker_modal_read action "  action ❯ " || return 0
    case "$action" in
      "") return ;;
      1)
        run_prompt_quarantine_action ingest "$key" || true
        return
        ;;
      2)
        picker_modal_read resume_dir "  Absolute project directory (Enter: current directory) ❯ " || return 0
        if [[ -z $resume_dir ]]; then
          resume_dir=$(pwd -P)
        fi
        if [[ $resume_dir != /* ]]; then
          echo "  Use an absolute project directory. Nothing changed."
          continue
        fi
        run_prompt_quarantine_action resume "$key" "$resume_dir" || true
        return
        ;;
      3)
        picker_modal_read answer "  Type discard to confirm ❯ " || return 0
        if [[ ${answer,,} != discard ]]; then
          echo "  Prompt kept. Nothing changed."
          continue
        fi
        run_prompt_quarantine_action discard "$key" || true
        CONFIRM_FORGIVE=1
        return
        ;;
      4)
        prompt_quarantine_prune
        return
        ;;
      *) echo "  Unknown choice. Nothing changed." ;;
    esac
  done
}

picker_mark_seen() {
  local provider=$1 uuid=$2 confidence=$3
  [[ ( $provider == claude || $provider == codex ) &&
     $confidence == exact && -n $uuid ]] || return 0
  python3 "$SK_INVENTORY_CORE" msg queue \
    --mark-seen "$provider:$uuid" >/dev/null 2>&1 || true
}

create_proof() {
  local number=$1
  python3 - "$VIEW" "$number" "$SK_STATE_DIR" <<'PY'
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

view_path, number_text, state_dir = sys.argv[1:4]
with open(view_path, encoding="utf-8") as handle:
    data = json.load(handle)
if data.get("stale") or data.get("source") != "live":
    raise SystemExit(3)
wanted = int(number_text)
matches = [
    row
    for row in data.get("sessions", [])
    if row.get("terminal_number") == wanted
]
if len(matches) != 1:
    raise SystemExit(2)
row = matches[0]
if row.get("mutation_allowed") is not True:
    raise SystemExit(3)
identity = row.get("identity") or {}
shell = row.get("shpool_shell") or {}
daemon = data.get("daemon_generation") or {}
proof = {
    "daemon_pid": daemon.get("pid"),
    "daemon_process_start_ticks": daemon.get("process_start_ticks"),
    "proof_type": "session-kit-picker-session-v1",
    "provider": row.get("provider"),
    "provider_pid": identity.get("pid"),
    "provider_process_start_ticks": identity.get("process_start_ticks"),
    "schema_version": 1,
    "shell_pid": shell.get("pid"),
    "shell_process_start_ticks": shell.get("process_start_ticks"),
    "shpool_id": row.get("shpool_id_raw"),
    "started_at_unix_ms": row.get("started_at_unix_ms"),
    "uuid": identity.get("uuid") or "",
}
payload = (json.dumps(proof, sort_keys=True, separators=(",", ":")) + "\n").encode()
descriptor, name = tempfile.mkstemp(prefix="picker-proof.", suffix=".json", dir=state_dir)
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        descriptor = -1
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    metadata = os.lstat(name)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.geteuid()
    ):
        raise OSError("unsafe proof")
    print(name)
except BaseException:
    if descriptor >= 0:
        os.close(descriptor)
    try:
        os.unlink(name)
    except OSError:
        pass
    raise
PY
}

run_proof_action() {
  local command=$1 number=$2
  shift 2
  local proof status
  proof=$(create_proof "$number") || {
    echo "  The displayed session is stale or unsafe. Nothing changed."
    return 1
  }
  TEMP_FILES+=("$proof")
  "$SP_CMD" "$command" "$proof" "$@"
  status=$?
  command rm -f -- "$proof"
  if (( status != 0 )); then
    # The caller explains the failure itself when it has something better to
    # say than a bare refusal.
    if [[ ${PROOF_ACTION_QUIET:-0} != 1 ]]; then
      echo "  The action was refused. Nothing changed."
    fi
    return "$status"
  fi
}

refresh_after_action() {
  # A failed refresh must never kill the picker: the previous snapshot stays
  # on screen (marked stale, actions disabled) and the next loop retries.
  if ! refresh_snapshot; then
    sk_log_action picker_refresh failed || true
    echo "  Live sessions could not be refreshed; showing the previous view."
  fi
}

change_account_number() {
  local number=$1 provider=$2 choices answer alias
  new_temp account-switch-choices || return
  choices=$NEW_TEMP
  if ! "$SP_CMD" account choices "$provider" >"$choices" 2>/dev/null; then
    echo "  Account choices are unavailable. Nothing changed."
    return
  fi
  echo
  python3 - "$choices" <<'PY'
import json,sys
data=json.load(open(sys.argv[1], encoding="utf-8"))
print("  Change to account")
for number,row in enumerate(data.get("choices",[]),1):
    state="ready" if row.get("eligible") else str(row.get("health") or "unverified")
    plan=f" | {row.get('plan')}" if row.get("plan") else ""
    print(f"  {number:2}  {row.get('alias')}: {row.get('email')}{plan} | {state}")
print("  Enter  Back")
PY
  echo
  picker_modal_read answer "  target account ❯ " || return
  [[ -n $answer ]] || return
  alias=$(python3 - "$choices" "$answer" <<'PY'
import json,sys
rows=json.load(open(sys.argv[1], encoding="utf-8")).get("choices",[])
choice=sys.argv[2].strip()
if not choice.isdigit() or not 1 <= int(choice) <= len(rows):
    raise SystemExit(2)
selected=rows[int(choice)-1]
if selected.get("eligible") is not True:
    raise SystemExit(2)
print(selected.get("alias", ""))
PY
  ) || {
    echo "  Choose a healthy account number shown above. Nothing changed."
    return
  }
  echo
  printf '  Change terminal %s to account %s?\n' "$number" "$alias"
  local confirm
  picker_modal_read confirm "  Type switch to confirm ❯ " || return
  if [[ ${confirm,,} != switch ]]; then
    echo "  Account change cancelled."
    return
  fi
  run_proof_action picker-account-switch "$number" "$alias" || true
  CONFIRM_FORGIVE=1
  refresh_after_action
}

choose_number() {
  local number=$1 metadata
  require_live_actions || return
  number_on_page "$number" || {
    echo "  Choose a number shown here. Nothing changed."
    return
  }
  metadata=$(number_metadata "$number") || {
    echo "  The displayed row changed format. Nothing changed."
    return
  }
  local -a fields=()
  mapfile -t fields <<<"$metadata"
  if (( ${#fields[@]} != 8 )); then
    echo "  The displayed row changed format. Nothing changed."
    return
  fi
  local availability mutation display_id provider title title_state uuid confidence
  availability=$(decode64 "${fields[0]}")
  mutation=$(decode64 "${fields[1]}")
  display_id=$(decode64 "${fields[2]}")
  provider=$(decode64 "${fields[3]}")
  title=$(decode64 "${fields[4]}")
  uuid=$(decode64 "${fields[6]}")
  confidence=$(decode64 "${fields[7]}")
  title_state=$(python3 - "$VIEW" "$number" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    rows = json.load(handle).get("sessions", [])
matches = [row for row in rows if row.get("terminal_number") == int(sys.argv[2])]
print(str(matches[0].get("provider_title_state") or "") if len(matches) == 1 else "")
PY
) || title_state=""
  if [[ $mutation != true ]]; then
    printf '  Session %s (%s) is display-only. Nothing changed.\n' "$number" "$title"
    return
  fi
  if [[ $availability == ready ]]; then
    # The window's tab carries the thread name for the whole attachment —
    # the only in-window name surface Codex has (Claude's own title hook
    # re-asserts the same name in-session). Cleared again on return.
    if [[ -t 1 && -n $title ]]; then
      printf '\033]0;%s\007' "$title"
    fi
    local open_status=0
    picker_mark_seen "$provider" "$uuid" "$confidence"
    PROOF_ACTION_QUIET=1 run_proof_action picker-open "$number" || open_status=$?
    # The attachment (or its failed attempt) owned the screen; start the
    # menu from a clean frame.
    picker_clear_screen
    case "$open_status" in
      0) ;;
      "$PICKER_REFUSED_STATUS")
        echo "  The displayed session changed or failed a safety check. Nothing changed."
        ;;
      "$PICKER_ATTACH_FAILED_STATUS")
        echo "  The session manager could not connect. This does not prove the session is dead."
        echo "  The session was left alone; refresh, inspect its history, or try again."
        ;;
      *)
        echo "  The open command failed without a verified cause. The session was left alone."
        ;;
    esac
    if [[ -t 1 ]]; then
      printf '\033]0;%s\007' "session kit"
    fi
    refresh_after_action
    return
  fi

  while true; do
    echo
    printf '  %s [session %s]\n' "$title" "$number"
    echo "  Already open in another SSH window."
    echo
    echo "  1  Move it here (the other window disconnects)"
    echo "  2  View full terminal history"
    echo "  3  Close it (the shell and everything inside will end)"
    if [[ $provider == claude || $provider == codex ]]; then
      echo "  4  Change subscription account (keeps this exact thread)"
    fi
    if [[ $provider == codex && $title_state == pending ]]; then
      echo "  5  Apply the pending title (restarts only a proven-idle Codex provider)"
    fi
    echo "  Enter  Back"
    echo
    local action
    picker_modal_read action "  action ❯ " || return 0
    case "$action" in
      "") return ;;
      1)
        picker_mark_seen "$provider" "$uuid" "$confidence"
        run_proof_action picker-takeover "$number" || true
        CONFIRM_FORGIVE=1
        refresh_after_action
        return
        ;;
      2)
        run_proof_action picker-history "$number" || true
        refresh_after_action
        return
        ;;
      3)
        run_proof_action picker-close "$number" || true
        CONFIRM_FORGIVE=1
        refresh_after_action
        return
        ;;
      4)
        if [[ $provider == claude || $provider == codex ]]; then
          change_account_number "$number" "$provider"
          return
        fi
        echo "  Only Claude and Codex threads can change account. Nothing changed."
        ;;
      5)
        if [[ $provider == codex && $title_state == pending ]]; then
          run_proof_action picker-title-refresh "$number" || true
          refresh_after_action
          return
        fi
        echo "  That session has no pending Codex title. Nothing changed."
        ;;
      *) echo "  Unknown choice. Nothing changed." ;;
    esac
  done
}

# The one selection grammar: k 5 · k 5, 6, 8 · k 4-7 · k all. Every number is
# validated against the CURRENT page before anything closes; one bad token
# refuses the whole request so a typo never kills a neighbor.
direct_close() {
  local raw=$1 token
  local -a numbers=()
  if [[ ${raw,,} == a || ${raw,,} == all ]]; then
    local -a shown=()
    mapfile -t shown < <(page_numbers)
    (( ${#shown[@]} > 0 )) || {
      echo "  Use k with visible numbers (k 5, 6, 8). Nothing changed."
      return
    }
    numbers=("${shown[@]}")
  else
  raw=${raw//,/ }
  for token in $raw; do
    if [[ $token =~ ^[0-9]+$ ]]; then
      numbers+=("$token")
    elif [[ $token =~ ^([0-9]+)-([0-9]+)$ ]]; then
      local first=${BASH_REMATCH[1]} last=${BASH_REMATCH[2]}
      if (( first > last || last - first > 98 )); then
        echo "  Use k with visible numbers or small ranges. Nothing changed."
        return
      fi
      local expanded
      for (( expanded=first; expanded<=last; expanded++ )); do
        numbers+=("$expanded")
      done
    else
      echo "  Use k with visible numbers (k 5, 6, 8). Nothing changed."
      return
    fi
  done
  fi
  (( ${#numbers[@]} > 0 )) || {
    echo "  Use k with visible numbers (k 5, 6, 8). Nothing changed."
    return
  }
  require_live_actions || return
  local number
  for number in "${numbers[@]}"; do
    number_on_page "$number" || {
      echo "  $number is not shown here. Nothing changed."
      return
    }
  done
  for number in "${numbers[@]}"; do
    run_proof_action picker-close "$number" || true
  done
  CONFIRM_FORGIVE=1
  refresh_after_action
}

rename_number() {
  local number=$1 metadata provider
  require_live_actions || return
  number_on_page "$number" || {
    echo "  Choose a number shown here. Nothing changed."
    return
  }
  metadata=$(number_metadata "$number") || return
  local -a fields=()
  mapfile -t fields <<<"$metadata"
  if (( ${#fields[@]} != 8 )); then
    echo "  The displayed row changed format. Nothing changed."
    return
  fi
  provider=$(decode64 "${fields[3]}")
  if [[ $provider != claude && $provider != codex ]]; then
    echo "  Only Claude and Codex conversations can have a custom name."
    return
  fi
  local title
  picker_modal_read title "  New name (Enter: cancel) ❯ " || return 0
  [[ -n ${title//[[:space:]]/} ]] || {
    echo "  Rename cancelled."
    return
  }
  run_proof_action picker-name "$number" "$title" || true
  refresh_after_action
}

reset_name_number() {
  local number=$1 metadata provider uuid confidence
  require_live_actions || return
  number_on_page "$number" || {
    echo "  Choose a number shown here. Nothing changed."
    return
  }
  metadata=$(number_metadata "$number") || return
  local -a fields=()
  mapfile -t fields <<<"$metadata"
  if (( ${#fields[@]} != 8 )); then
    echo "  The displayed row changed format. Nothing changed."
    return
  fi
  provider=$(decode64 "${fields[3]}")
  uuid=$(decode64 "${fields[6]}")
  confidence=$(decode64 "${fields[7]}")
  if [[ ( $provider != claude && $provider != codex ) ||
        $confidence != exact || -z $uuid ]]; then
    echo "  Only exact Claude and Codex conversations can reset a custom name."
    return
  fi
  run_proof_action picker-name-reset "$number" || true
  refresh_after_action
}

fork_number() {
  local number=$1 metadata provider uuid confidence
  require_live_actions || return
  number_on_page "$number" || {
    echo "  Choose a number shown here. Nothing changed."
    return
  }
  metadata=$(number_metadata "$number") || return
  local -a fields=()
  mapfile -t fields <<<"$metadata"
  if (( ${#fields[@]} != 8 )); then
    echo "  The displayed row changed format. Nothing changed."
    return
  fi
  provider=$(decode64 "${fields[3]}")
  uuid=$(decode64 "${fields[6]}")
  confidence=$(decode64 "${fields[7]}")
  if [[ ( $provider != claude && $provider != codex ) ||
        $confidence != exact || -z $uuid ]]; then
    echo "  Only exact Claude and Codex conversations can be forked."
    return
  fi
  run_proof_action picker-fork "$number" || true
  refresh_after_action
}

# Open the report a chosen reply belongs to. The row's position is the only
# thing a person typed; the message id comes from the index the same redraw
# built, so a reply that arrived since cannot shift what r2 means mid-keypress.
open_reply_row() {
  local wanted=$1 line key msg_id=""
  [[ $wanted =~ ^[0-9]+$ ]] || {
    echo "  Choose a reply shown here, such as r1. Nothing changed."
    return
  }
  while IFS=$'\t' read -r key line; do
    [[ $key == "r$wanted" ]] || continue
    msg_id=$line
    break
  done <<<"$MSG_REPLY_INDEX"
  if [[ -z $msg_id ]]; then
    echo "  r$wanted is not a reply shown here. Nothing changed."
    return
  fi
  require_live_actions || return
  "$SP_CMD" msg report "$msg_id" || true
  picker_clear_screen
  CONFIRM_FORGIVE=1
}

# ---- peek, jump, and view toggles --------------------------------------
# A row says a session needs a reply. It does not say what the session asked,
# and finding that out meant attaching to it -- which marks it seen, moves the
# window, and loses the list. Peek shows the question and the last messages in
# place, and answers from there through the same `sp msg` the message centre
# uses, so there is no second delivery path to keep in step.
peek_row() {
  local wanted=$1 cols answer status
  [[ $wanted =~ ^[0-9]+$ ]] || {
    echo "  Choose a session number shown here, such as i3. Nothing changed."
    return
  }
  number_on_page "$wanted" || {
    echo "  Choose a number shown here. Nothing changed."
    return
  }
  cols=${COLUMNS:-}
  [[ $cols =~ ^[0-9]+$ ]] || cols=$(tput cols 2>/dev/null || printf '100')
  [[ $cols =~ ^[0-9]+$ ]] || cols=100
  while true; do
    local card=""
    status=0
    card=$(python3 "$PEEK_TOOL" --view "$VIEW" --snapshot "$SNAPSHOT" \
      --state "$SK_STATE_DIR" --number "$wanted" --width "$cols" 2>/dev/null) ||
      status=$?
    if (( status != 0 )) || [[ -z $card ]]; then
      echo "  That session's details could not be read. Nothing changed."
      return
    fi
    printf '%s\n' "$card"
    echo
    echo "  Type a reply and press Enter · o: open the session · Enter: back"
    echo
    picker_modal_read answer "  peek $wanted ❯ " || return 0
    case "$answer" in
      "")
        # The card owned the screen; hand the list back a clean frame, the
        # same way the message report and the message centre do. A reply is
        # deliberately NOT cleared: its receipts are worth reading.
        picker_clear_screen
        return 0
        ;;
      o|O)
        choose_number "$wanted"
        return 0
        ;;
      *)
        peek_reply "$wanted" "$answer"
        return 0
        ;;
    esac
  done
}

# The reply itself is `sp msg <number> "text"` and nothing else: the same
# resolution, the same ledger, the same receipts a person sees when they write
# from the message centre or the command line.
peek_reply() {
  local number=$1 text=$2
  require_live_actions || return 0
  if ! "$SP_CMD" msg "$number" "$text"; then
    echo "  The message was not sent. Nothing changed."
    return 0
  fi
  printf '  Sent to session %s.\n' "$number"
  CONFIRM_FORGIVE=1
}

# Every listed session that is waiting on a person or has finished unopened,
# in the order the page shows them, with the row's position so the caller can
# turn to the page it is on.
picker_attention_numbers() {
  python3 - "$VIEW" 2>/dev/null <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        rows = json.load(handle).get("sessions", [])
except (OSError, ValueError):
    raise SystemExit(0)
for index, row in enumerate(rows):
    if not isinstance(row, dict):
        continue
    number = row.get("terminal_number")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        continue
    if (
        row.get("needs_you")
        or row.get("setup_incomplete")
        or row.get("_picker_attention_bucket")
    ):
        print(f"{number}\t{index}")
PY
}

# Move to the next session that wants a person, wrapping at the end. The row
# is marked rather than selected: nothing is opened, attached, or acknowledged
# by finding it.
jump_next_attention() {
  local line number index chosen="" chosen_index=0 first="" first_index=0
  local seen_current=0
  while IFS=$'\t' read -r number index; do
    [[ $number =~ ^[0-9]+$ && $index =~ ^[0-9]+$ ]] || continue
    if [[ -z $first ]]; then
      first=$number
      first_index=$index
    fi
    if [[ -z $PICKER_JUMP_NUMBER ]]; then
      chosen=$number
      chosen_index=$index
      break
    fi
    if (( seen_current )) && [[ -z $chosen ]]; then
      chosen=$number
      chosen_index=$index
      break
    fi
    [[ $number == "$PICKER_JUMP_NUMBER" ]] && seen_current=1
  done < <(picker_attention_numbers)
  if [[ -z $chosen ]]; then
    if [[ -z $first ]]; then
      PICKER_JUMP_NUMBER=""
      echo "  No listed session is waiting on you."
      return
    fi
    # Past the last one, or the marked row is gone: start again at the top.
    chosen=$first
    chosen_index=$first_index
  fi
  PICKER_JUMP_NUMBER=$chosen
  (( PAGE_SIZE >= 1 )) || PAGE_SIZE=1
  PAGE=$(( chosen_index / PAGE_SIZE + 1 ))
  clamp_page
  printf '  Session %s wants you — open with %s, peek with %s.\n' \
    "$chosen" "$(picker_green "$chosen")" "$(picker_green "i$chosen")"
}

# Grouping decides which rows sit together, never which rows exist. State is
# the default the picker has always drawn; the other two are opt-in for one
# session of the picker and reset when it is next started.
cycle_grouping() {
  local requested=${1:-} mode previous
  case "${requested,,}" in
    "")
      case "$(picker_group_mode)" in
        state) mode=provider ;;
        provider) mode=project ;;
        *) mode=state ;;
      esac
      ;;
    state) mode=state ;;
    provider) mode=provider ;;
    project) mode=project ;;
    *)
      echo "  Grouping is state, provider, or project. Nothing changed."
      return
      ;;
  esac
  previous=${PICKER_GROUP_MODE:-state}
  PICKER_GROUP_MODE=$mode
  if ! build_view; then
    PICKER_GROUP_MODE=$previous
    build_view || {
      sk_log_action picker_refresh failed || true
      echo "  The list could not be rebuilt; showing the previous view."
      return
    }
    echo "  The list could not be regrouped. The grouping is unchanged."
    return
  fi
  PAGE=1
  printf '  Grouping by %s.\n' "$mode"
}

# Compact rows drop the group headings and the trailing spacer, and keep only
# the primary state beside each name. Same rows, same numbers, more of them.
toggle_compact() {
  if [[ ${PICKER_COMPACT:-0} == 1 ]]; then
    PICKER_COMPACT=0
    echo "  Compact rows off."
  else
    PICKER_COMPACT=1
    echo "  Compact rows on."
  fi
  PAGE=1
}

# Hand the window to the message centre. Writing a message, watching the
# answers, and going back to an older one all live there, in one surface that
# the command line reaches the same way -- this menu entry only opens it, so
# there is nothing here to drift out of step with it.
compose_message() {
  require_live_actions || return
  "$SP_CMD" msg || true
  # The centre owned the screen while it ran, and its own last act clears it;
  # start the menu from a clean frame either way. A stray Enter left by a
  # confirm inside it must not read as "give me a plain terminal".
  picker_clear_screen
  CONFIRM_FORGIVE=1
}

# The supervisor helper is supplied by the steward lane. During a partial
# rollout this key remains fail-open and returns to a usable picker.
open_supervisor() {
  local supervisor="$SCRIPT_DIR/supervisor" candidate owner
  if [[ -n ${SESSION_KIT_SUPERVISOR_CMD:-} ]]; then
    candidate=$SESSION_KIT_SUPERVISOR_CMD
    owner=$(stat -c '%u' -- "$candidate" 2>/dev/null) || owner=""
    if [[ $candidate == /* && ! -L $candidate && -f $candidate &&
          -x $candidate && $owner == "$(id -u)" ]]; then
      supervisor=$candidate
    fi
  fi
  if [[ -L $supervisor || ! -f $supervisor || ! -x $supervisor ]]; then
    echo "  The fleet supervisor is not installed yet. Nothing changed."
    return 0
  fi
  # Creation is bounded — a wedged ensure must hand the picker back. The
  # attach is interactive and runs unbounded in the foreground, exactly like
  # every other picker attach; a timeout here would kill the live session.
  # `supervisor ensure` reports to its caller in identifiers — the session it
  # created, the receipt of the brief it sent. That is a machine's report, and
  # the picker is a person's screen: keep it off the screen and say what
  # happened instead.
  local ensure_log
  ensure_log=$(mktemp "${TMPDIR:-/tmp}/session-kit-supervisor-ensure.XXXXXX") || {
    echo "  The fleet supervisor could not be started. Nothing else changed."
    return 0
  }
  chmod 600 "$ensure_log" 2>/dev/null || true
  if ! sk_timeout "${SESSION_KIT_SUPERVISOR_ENSURE_TIMEOUT:-20}" "$supervisor" ensure \
       >"$ensure_log" 2>&1; then
    command rm -f -- "$ensure_log"
    echo "  The fleet supervisor could not be started. Nothing else changed."
    return 0
  fi
  command rm -f -- "$ensure_log"
  if ! "$supervisor" open; then
    echo "  The fleet supervisor could not be opened. Nothing else changed."
    return 0
  fi
  picker_clear_screen
  refresh_after_action
}

project_file() {
  local destination=$1
  python3 - "$SK_PROJECTS_FILE" "$destination" <<'PY'
import json
import os
from pathlib import Path
import re
import sys
import unicodedata

source, destination = map(Path, sys.argv[1:3])
projects = []
# The provider is already chosen before this list is shown, so two configured
# rows that differ only by provider would render as identical lines. Keep the
# first row for each directory and show it under its configured alias.
listed = set()
if source.is_file() and not source.is_symlink():
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 3:
            continue
        alias, configured_provider, cwd = fields[:3]
        if (
            re.fullmatch(r"[a-z0-9_-]+", alias)
            and configured_provider in {"claude", "codex", "shell"}
            and cwd.startswith("/")
            and not any(
                unicodedata.category(character).startswith("C")
                for character in cwd
            )
            and os.path.isdir(cwd)
        ):
            try:
                key = os.path.realpath(cwd)
            except OSError:
                key = cwd
            if key in listed:
                continue
            listed.add(key)
            projects.append({"alias": alias, "cwd": cwd})
default = next((index for index, row in enumerate(projects, 1) if row["alias"] == "sl"), 1 if projects else None)
with destination.open("w", encoding="utf-8") as handle:
    json.dump({"projects": projects, "default": default}, handle, sort_keys=True, separators=(",", ":"))
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(destination, 0o600)
PY
}

guided_account() {
  local provider=$1 choices answer selected
  GUIDED_ACCOUNT=
  new_temp login-accounts || return 1
  choices=$NEW_TEMP
  if ! "$SP_CMD" account choices "$provider" >"$choices" 2>/dev/null; then
    echo "  Account choices are unavailable. Nothing started."
    return 1
  fi
  echo
  python3 - "$choices" <<'PY'
import json
import sys

data=json.load(open(sys.argv[1], encoding="utf-8"))
rows=data.get("choices", [])
print("  Account")
for number,row in enumerate(rows,1):
    state="ready" if row.get("eligible") else str(row.get("health") or "unverified")
    mark=" | recommended" if row.get("recommended") else ""
    plan=f" | {row.get('plan')}" if row.get("plan") else ""
    print(f"  {number:2}  {row.get('alias')}: {row.get('email')}{plan} | {state}{mark}")
recommendation=data.get("recommendation")
if recommendation:
    print(f"  Enter  Use {recommendation} | b  Back")
else:
    print("  Choose a number | b  Back")
PY
  local count
  count=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("choices",[])))' "$choices") || return 1
  if [[ $count == 0 ]]; then
    printf '  No %s account is enrolled. Use sp account adopt-default or sp account enroll first.\n' "$provider"
    return 1
  fi
  echo
  picker_modal_read answer "  account ❯ " || return 1
  [[ $answer != b && $answer != B ]] || return 1
  selected=$(python3 - "$choices" "$answer" <<'PY'
import json
import sys

data=json.load(open(sys.argv[1], encoding="utf-8"))
choice=sys.argv[2].strip()
rows=data.get("choices", [])
if not choice:
    recommendation=data.get("recommendation")
    if not recommendation:
        raise SystemExit(2)
    print(recommendation)
    raise SystemExit(0)
if not choice.isdigit() or not 1 <= int(choice) <= len(rows):
    raise SystemExit(2)
selected=rows[int(choice)-1]
if selected.get("eligible") is not True:
    raise SystemExit(2)
print(selected.get("alias", ""))
PY
  ) || {
    echo "  Choose a healthy account number shown above. Nothing started."
    return 1
  }
  [[ $selected =~ ^[a-z][a-z0-9_-]{0,11}$ ]] || return 1
  GUIDED_ACCOUNT=$selected
}

guided_new() {
  require_live_actions || return
  echo
  echo "  New session"
  echo "  1  Claude Code"
  echo "  2  Codex"
  echo "  3  Regular managed shell"
  echo "  Enter  Back"
  echo
  local answer provider
  picker_modal_read answer "  provider ❯ " || return 0
  case "$answer" in
    "") return ;;
    1) provider=claude ;;
    2) provider=codex ;;
    3) provider=shell ;;
    *) echo "  Unknown choice. Nothing changed."; return ;;
  esac

  local account_alias=
  if [[ $provider == claude || $provider == codex ]]; then
    guided_account "$provider" || return
    account_alias=$GUIDED_ACCOUNT
  fi

  local projects
  new_temp login-projects || return
  projects=$NEW_TEMP
  project_file "$projects" || {
    echo "  Project list is unavailable. Nothing started."
    return
  }
  echo
  python3 - "$projects" <<'PY'
import json,sys,unicodedata
d=json.load(open(sys.argv[1]))
default=d.get("default")
print("  Project")
for number,row in enumerate(d.get("projects",[]),1):
    print(f"  {number:2}  {row['alias']}: {row['cwd']}")
if default:
    row=d["projects"][default-1]
    print(f"  Enter  Use {row['alias']}: {row['cwd']} | b  Back")
else:
    print("  Enter  Current directory | b  Back")
PY
  echo
  local project_choice project_alias
  picker_modal_read project_choice "  project ❯ " || return 0
  [[ $project_choice != b && $project_choice != B ]] || return
  project_alias=$(python3 - "$projects" "$project_choice" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
choice=sys.argv[2].strip()
if not choice:
    default=d.get("default")
    if not default:
        print("")
        raise SystemExit(0)
    choice=str(default)
if not choice.isdigit():
    raise SystemExit(2)
number=int(choice)
rows=d.get("projects",[])
if number < 1 or number > len(rows):
    raise SystemExit(2)
print(rows[number-1]["alias"])
PY
) || {
    echo "  Unknown project. Nothing started."
    return
  }
  if [[ $provider == claude || $provider == codex ]]; then
    echo "  After exact startup proof, use name <number> to assign an optional conversation name."
  fi
  if [[ -n $project_alias ]]; then
    if [[ -n $account_alias ]]; then
      "$SP_CMD" new "$provider" "$project_alias" --account "$account_alias" || true
    else
      "$SP_CMD" new "$provider" "$project_alias" || true
    fi
  else
    if [[ -n $account_alias ]]; then
      "$SP_CMD" new "$provider" --account "$account_alias" || true
    else
      "$SP_CMD" new "$provider" || true
    fi
  fi
  refresh_after_action
}
