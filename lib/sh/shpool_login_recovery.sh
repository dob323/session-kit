#!/usr/bin/env bash
# Picker recovery review: pending entries left by an interrupted session and
# the live target a recovered conversation belongs to. Source this file; do not
# execute it.
#
# Source order: bin/shpool_login sources this module ahead of its first trap.
# These functions read RECOVERY, SNAPSHOT, and SP_CMD, assigned in that file
# before the boot sequence runs, and call into the theme, live, and view
# modules.
#
# Globals the entry script owns are assigned there, not here.
# shellcheck disable=SC2154

recovery_list() {
  local fresh
  new_temp login-recovery || return 1
  fresh=$NEW_TEMP
  "$STATUS_CMD" --recovery-pending-list > "$fresh" || {
    command rm -- "$fresh"
    return 1
  }
  RECOVERY=$fresh
}

recovery_count() {
  local payload
  payload=$("$STATUS_CMD" --recovery-pending-list) || return 1
  python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("entries",[])))' \
    <<<"$payload"
}

active_target_for_uuid() {
  local provider=$1 uuid=$2
  python3 - "$SNAPSHOT" "$provider" "$uuid" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
wanted_provider = sys.argv[2]
wanted = sys.argv[3].casefold()
managed = []
outside = []
for row in data.get("sessions", []):
    identity = row.get("identity") or {}
    if (
        row.get("provider") == wanted_provider
        and str(identity.get("uuid") or "").casefold() == wanted
        and identity.get("confidence") == "exact"
    ):
        number = row.get("terminal_number")
        if (
            isinstance(number, int)
            and not isinstance(number, bool)
            and number > 0
        ):
            managed.append(number)
for row in data.get("outside_agents", []):
    identity = row.get("identity") or {}
    if (
        row.get("provider") == wanted_provider
        and str(identity.get("uuid") or "").casefold() == wanted
        and identity.get("confidence") == "exact"
    ):
        outside.append(row)
if len(managed) == 1 and not outside:
    print(f"managed:{managed[0]}")
elif not managed and len(outside) == 1:
    print("outside")
elif managed or outside:
    print("ambiguous")
else:
    print("none")
PY
}

review_recovery() {
  # Every early exit returns 0: each path below already explains itself, and
  # a nonzero status made the More menu stack "Nothing to review." on top.
  require_live_actions || return 0
  recovery_list || {
    echo "  Recovery records are unavailable. Nothing changed."
    return 0
  }
  local count
  count=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("entries",[])))' "$RECOVERY")
  if (( count == 0 )); then
    echo "  No conversations are waiting for recovery."
    return 0
  fi
  echo
  echo "  Conversations available for exact recovery"
  python3 - "$RECOVERY" <<'PY'
import datetime
import json
import re
import sys
import time
import unicodedata

def started_at(row):
    value = row.get("started_at_unix_ms")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value / 1000
    # Older records predate the timestamp field. Generated internal IDs contain
    # the local login time, so use one only as legacy input and never print it.
    match = re.fullmatch(
        r"s(\d{8})-(\d{6})-\d+", str(row.get("old_shpool_id") or "")
    )
    if not match:
        return None
    try:
        value = datetime.datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return time.mktime(value.timetuple())

def login_age(row):
    started = started_at(row)
    if started is None:
        return "login time unknown"
    hours = max(0, int((time.time() - started) // 3600))
    if hours < 1:
        return "logged in less than 1h ago"
    days, remaining = divmod(hours, 24)
    if days:
        return f"logged in {days}d {remaining}h ago"
    return f"logged in {hours}h ago"

for number,row in enumerate(json.load(open(sys.argv[1])).get("entries",[]),1):
    provider=str(row.get("provider") or "unknown").title()
    clean=lambda value: " ".join(
        "".join(
            " " if unicodedata.category(character).startswith("C") else character
            for character in str(value or "")
        ).split()
    )
    title=clean(row.get("title"))
    conflicts=", ".join(clean(value) for value in row.get("conflict_fields",[]))
    suffix=f" [review required: conflicting {conflicts}]" if conflicts else ""
    print(f"  {number:2}  [{provider}] {title} [{login_age(row)}]{suffix}")
PY
  echo
  echo "  1,3,5 or 2-4: restore selected | all (or a): restore every conversation"
  echo "  Enter: Back"
  echo
  local answer selected
  picker_modal_read answer "  recovery ❯ " || return 0
  [[ -n $answer ]] || return 0
  selected=$(python3 - "$RECOVERY" "$answer" "$SCRIPT_DIR/../lib" <<'PY'
import json,sys
sys.path.insert(0, sys.argv[3])
from sessionkit_inventory.common import CollectionError, parse_number_selection
entries=json.load(open(sys.argv[1])).get("entries",[])
try:
    chosen=parse_number_selection(sys.argv[2], len(entries))
except CollectionError:
    raise SystemExit(2)
if not chosen:
    raise SystemExit(2)
print("\n".join(map(str,chosen)))
PY
) || {
    echo "  Invalid recovery selection. Nothing restored."
    return
  }

  local number encoded generation old_id provider uuid cwd actionable conflicts launched attempt acknowledged active_target active_number
  local -a values=()
  # The selection rides on fd 3 so the loop body keeps the terminal on stdin:
  # with the selection on stdin, choose_number's menu read consumed the NEXT
  # selected recovery number as its action (or hit EOF and killed the picker
  # mid-recovery), and sp's attach saw a here-string instead of a tty.
  while IFS= read -r -u 3 number; do
    [[ $number =~ ^[0-9]+$ ]] || continue
    encoded=$(python3 - "$RECOVERY" "$number" <<'PY'
import base64,json,sys
row=json.load(open(sys.argv[1])).get("entries",[])[int(sys.argv[2])-1]
for key in ("source_generation_key","old_shpool_id","provider","uuid","cwd"):
    print(base64.b64encode(str(row.get(key) or "").encode()).decode())
print(base64.b64encode(", ".join(map(str,row.get("conflict_fields",[]))).encode()).decode())
# Keep the nonempty actionable flag last. Bash command substitution strips
# trailing blank lines, so an empty conflicts field cannot safely be last.
print(base64.b64encode(str(row.get("actionable",True)).lower().encode()).decode())
PY
)
    mapfile -t values <<<"$encoded"
    if (( ${#values[@]} != 7 )); then
      printf '  Recovery record %s is incomplete; it was retained.\n' "$number" >&2
      continue
    fi
    generation=$(decode64 "${values[0]}")
    old_id=$(decode64 "${values[1]}")
    provider=$(decode64 "${values[2]}")
    uuid=$(decode64 "${values[3]}")
    cwd=$(decode64 "${values[4]}")
    conflicts=$(decode64 "${values[5]}")
    actionable=$(decode64 "${values[6]}")
    if [[ -z $generation || -z $old_id || -z $provider || -z $uuid || -z $cwd ]]; then
      printf '  Recovery record %s is incomplete; it was retained.\n' "$number" >&2
      continue
    fi
    if [[ $actionable != true ]]; then
      printf '  Recovery record %s has conflicting launch metadata (%s); it requires manual review and was retained.\n' \
        "$number" "${conflicts:-unknown fields}" >&2
      continue
    fi
    active_target=$(active_target_for_uuid "$provider" "$uuid") || active_target=ambiguous
    case "$active_target" in
      managed:*)
        active_number=${active_target#managed:}
        if "$STATUS_CMD" --recovery-pending-ack \
          "$generation" "$old_id" "$uuid" >/dev/null 2>&1; then
          printf '  Cleared the recovery record for exact active session %s.\n' \
            "$active_number"
        else
          printf '  Exact active session %s was found, but its recovery record could not be cleared; the record was retained.\n' \
            "$active_number" >&2
        fi
        printf '  Recovery item %s is already active as session %s; opening the existing session instead.\n' \
          "$number" "$active_number"
        printf '  Use fork %s from the picker to start a separate writable fork.\n' \
          "$active_number"
        QUERY=$uuid
        PAGE=1
        if build_view; then
          choose_number "$active_number"
        else
          echo "  The existing session could not be shown. Nothing restored."
        fi
        continue
        ;;
      outside)
        printf '  Recovery item %s is already active outside the session manager; no duplicate was started.\n' \
          "$number"
        continue
        ;;
      ambiguous)
        printf '  Active identity for recovery item %s is ambiguous; no duplicate was started.\n' \
          "$number"
        continue
        ;;
    esac
    if launched=$("$SP_CMD" restore-exact "$provider" "$uuid" "$cwd"); then
      printf '  Started recovery item %s.\n' "$number"
      acknowledged=0
      for attempt in {1..20}; do
        if "$STATUS_CMD" --recovery-pending-ack \
          "$generation" "$old_id" "$uuid" >/dev/null 2>&1; then
          acknowledged=1
          break
        fi
        sleep 0.25
      done
      if (( acknowledged == 0 )); then
        printf '  Exact live proof is pending for recovery item %s; its recovery record was retained.\n' "$number" >&2
      fi
    else
      printf '  Could not restore recovery item %s; its recovery record was retained.\n' "$number" >&2
    fi
  done 3<<<"$selected"
  refresh_after_action
}
