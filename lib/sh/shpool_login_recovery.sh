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
  # This screen draws and acts out of this one file, so a word cannot change
  # rows underneath it. `sp restore` has no such file -- it rebuilds the list
  # to act on it -- so what this screen is about to print is written down for
  # it to check a typed word against. A failure here loses that check and
  # nothing else, and it says so rather than passing for a screen that was
  # recorded.
  RECOVERY_UNRECORDED=""
  "$STATUS_CMD" --recovery-remember-printed "$fresh" >/dev/null || RECOVERY_UNRECORDED=1
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
    echo "  Closed sessions are unavailable. Nothing changed."
    return 0
  }
  local count
  count=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("entries",[])))' "$RECOVERY")
  if (( count == 0 )); then
    echo "  Closed sessions: none."
    return 0
  fi
  echo
  echo "  Closed sessions"
  python3 - "$RECOVERY" <<'PY'
import json
import sys
import time
import unicodedata

def event_age(row):
    """The age of the row's OWN event, in the words the other surface uses.

    This screen used to read three timestamp fields the one list does not
    emit, and fall back to a login time parsed out of an internal id -- so
    every row said "login time unknown" while `sp recover` printed a correct
    age for the same record from the same file.
    """
    value = row.get("when_unix_ms")
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return "time unknown"
    seconds = max(0, (int(time.time() * 1000) - value) // 1000)
    if seconds < 60:
        age = "under 1 min ago"
    elif seconds < 3600:
        age = f"{seconds // 60} min ago"
    elif seconds < 86400:
        age = f"{seconds // 3600} hr ago"
    else:
        days = seconds // 86400
        age = f"{days} day{'s' if days != 1 else ''} ago"
    return f"{'closed' if row.get('source') == 'closed' else 'lost'} {age}"

def clean(value):
    return " ".join(
        "".join(
            " " if unicodedata.category(character).startswith("C") else character
            for character in str(value or "")
        ).split()
    )

prepared=[]
for row in json.load(open(sys.argv[1])).get("entries",[]):
    provider=str(row.get("provider") or "unknown").title()
    # The session's own number, the one every other screen shows it under. A
    # conversation whose number was retired has none to show, and borrowing a
    # live session's would be worse than a dash -- it goes by the name beside
    # it, or by the time of its own event when two rows answer to one name.
    number=row.get("number")
    selector=str(row.get("selector") or "")
    if isinstance(number,int):
        label=f"{number:>4}"
    elif selector.startswith("@"):
        label=f"{selector:>4}"
    else:
        label="   —"
    title=clean(row.get("display_name")) or "unnamed"
    conflicts=", ".join(clean(value) for value in row.get("conflict_fields",[]))
    suffix=f" [review required: conflicting {conflicts}]" if conflicts else ""
    reason=clean(row.get("history_only_reason"))
    prepared.append((label, provider, title, event_age(row), suffix, reason))
for label, provider, title, age, suffix, reason in prepared:
    print(f"  {label}  [{provider}] {title} [{age}]{suffix}".rstrip())
    if reason:
        # Say it plainly rather than offering a restore that cannot work.
        print(f"        {reason}")
PY
  echo
  if [[ -n ${RECOVERY_UNRECORDED:-} ]]; then
    echo "  This screen could not be written down, so sp restore cannot check a word you type here against it."
  fi
  echo "  restore numbers · one with no number goes by the selector beside it · restore all a · ↵ back · b back"
  echo
  local answer selected
  picker_modal_read answer "  closed ❯ " || return 0
  # Back is back on every screen: without this, q read as a selection and came
  # back as "invalid recovery selection". `b` was the same hole, one letter
  # over -- it reached the selection parser and was refused as a bad number.
  [[ -n $answer ]] || return 0
  case "${answer,,}" in
    b|back|q|quit) return 0 ;;
  esac
  selected=$(python3 - "$RECOVERY" "$answer" "$SCRIPT_DIR/../lib" <<'PY'
import json,re,sys
sys.path.insert(0, sys.argv[3])
from sessionkit_inventory.common import CollectionError, parse_number_selection
entries=json.load(open(sys.argv[1])).get("entries",[])
clean=lambda value: " ".join(str(value or "").split())
# What a person types is what the row shows, and the list decides that once
# for both screens: the session's own number, or -- for a conversation whose
# number was retired -- the selector beside it. Neither is a row position, so
# neither moves when the list is rebuilt. The handle passed on to the loop is
# the row's own identity; no screen prints one and nothing accepts one as
# input.
rows=[]
for row in entries:
    number=row.get("number")
    handle=str(number) if isinstance(number,int) else str(row.get("short_id") or "")
    if not handle:
        continue
    rows.append({
        "handle": handle,
        "number": str(number) if isinstance(number,int) else "",
        "selector": clean(row.get("selector")).casefold(),
    })
if not rows:
    raise SystemExit(2)
text=clean(sys.argv[2])
folded=text.casefold()
if folded in {"a","all"}:
    print("\n".join(row["handle"] for row in rows))
    raise SystemExit(0)
# One rule, and the other surface applies the same one: a selector answers for
# exactly the row it is printed beside, or for nothing. The WHOLE word is
# tested first, because splitting it up before deciding what kind of answer it
# was made a name of digits and spaces read as a list of numbers: a
# conversation somebody called "2 4" restored sessions 2 and 4 here and the
# conversation named "2 4" on the other screen -- one typed word, two screens,
# two different conversations.
matched=[row for row in rows if row["selector"] and row["selector"] == folded]
if len(matched) > 1:
    raise SystemExit(3)
if not matched:
    # A rejected selection changes nothing. Catching the parser's refusal and
    # then adding back every fragment that happened to be a number meant
    # "2,garbage" restored session 2 and "2,,4" restored both -- a typo acting
    # as a command.
    parts=[]
    for chunk in text.split(","):
        pieces=chunk.split()
        if not pieces:
            raise SystemExit(2)
        parts.extend(pieces)
    if not parts:
        raise SystemExit(2)
    if all(re.fullmatch(r"\d+(-\d+)?", part) for part in parts):
        numbers=[int(row["number"]) for row in rows if row["number"]]
        if not numbers:
            raise SystemExit(2)
        asked=set()
        try:
            for part in parts:
                asked |= {
                    str(value) for value in parse_number_selection(part, max(numbers))
                }
        except CollectionError:
            raise SystemExit(2)
        matched=[row for row in rows if row["number"] in asked]
        # The module refuses to let two short ids stand for one another; a
        # number gets the same standard rather than first-match-wins.
        if len({row["number"] for row in matched}) != len(matched):
            raise SystemExit(3)
if not matched:
    raise SystemExit(2)
print("\n".join(row["handle"] for row in matched))
PY
) || {
    if (( $? == 3 )); then
      echo "  More than one closed session answers to that, so nothing was chosen."
    else
      echo "  There is no such closed session on this screen. Type what the row shows: its number, or the selector beside a session that has none."
    fi
    return
  }

  local handle label name number selector encoded generation old_id provider uuid cwd actionable conflicts reason launched attempt acknowledged active_target active_number
  local -a values=()
  # The selection rides on fd 3 so the loop body keeps the terminal on stdin:
  # with the selection on stdin, choose_number's menu read consumed the NEXT
  # selected recovery number as its action (or hit EOF and killed the picker
  # mid-recovery), and sp's attach saw a here-string instead of a tty.
  while IFS= read -r -u 3 handle; do
    [[ $handle =~ ^[0-9]+$ || $handle =~ ^[0-9a-f]{8,32}$ ]] || continue
    encoded=$(python3 - "$RECOVERY" "$handle" <<'PY'
import base64,json,sys
wanted=str(sys.argv[2])
def token(item):
    number=item.get("number")
    return str(number) if isinstance(number, int) else str(item.get("short_id") or "")
matching=[
    item
    for item in json.load(open(sys.argv[1])).get("entries",[])
    if token(item) == wanted
]
# One handle, one conversation. Two rows answering to it is a broken list, and
# acting on the first of them is how a person gets back a session they did not
# ask for.
if len(matching) != 1:
    raise SystemExit(1)
row=matching[0]
for key in ("source_generation_key","old_shpool_id","provider","uuid","cwd"):
    print(base64.b64encode(str(row.get(key) or "").encode()).decode())
number=row.get("number")
print(base64.b64encode((str(number) if isinstance(number,int) else "").encode()).decode())
print(base64.b64encode(" ".join(str(row.get("selector") or "").split()).encode()).decode())
print(base64.b64encode(", ".join(map(str,row.get("conflict_fields",[]))).encode()).decode())
print(base64.b64encode(str(row.get("history_only_reason") or "").encode()).decode())
print(base64.b64encode(" ".join(str(row.get("display_name") or "unnamed").split()).encode()).decode())
# Keep the nonempty restorable flag last. Bash command substitution strips
# trailing blank lines, so an empty reason field cannot safely be last.
print(base64.b64encode(str(bool(row.get("restorable"))).lower().encode()).decode())
PY
)
    mapfile -t values <<<"$encoded"
    if (( ${#values[@]} != 11 )); then
      # The handle is never shown: for a row with no number it is the
      # conversation's own id, and no screen prints one of those.
      echo "  That closed session is no longer on this list. Nothing changed." >&2
      continue
    fi
    generation=$(decode64 "${values[0]}")
    old_id=$(decode64 "${values[1]}")
    provider=$(decode64 "${values[2]}")
    uuid=$(decode64 "${values[3]}")
    cwd=$(decode64 "${values[4]}")
    number=$(decode64 "${values[5]}")
    selector=$(decode64 "${values[6]}")
    conflicts=$(decode64 "${values[7]}")
    reason=$(decode64 "${values[8]}")
    name=$(decode64 "${values[9]}")
    actionable=$(decode64 "${values[10]}")
    # A row is answered for by what the screen showed beside it -- its number,
    # or the selector it goes by. Never by the handle: for a row with no
    # number that is the conversation's own id, and a uuid that happens to
    # start with digits would read as a session number.
    if [[ -n $number ]]; then
      label=$number
    else
      label="\"${selector:-$name}\""
    fi
    # A row that cannot come back says why, in the sentence the list already
    # carries -- the same sentence `sp restore` gives for the same row. When
    # the records disagree, the fields that disagree are named too. This is
    # the only refusal for a row that is not restorable, so there is no second
    # branch below that no payload can reach.
    if [[ $actionable != true ]]; then
      if [[ -n $conflicts ]]; then
        printf '  Closed session %s: %s (%s). Nothing changed.\n' \
          "$label" "${reason:-its launch records disagree with each other}" \
          "$conflicts" >&2
      else
        printf '  Closed session %s keeps its history only: %s\n' \
          "$label" "${reason:-there is no conversation to reopen}" >&2
      fi
      continue
    fi
    # A conversation closed on purpose has no pending record and needs no
    # acknowledgment, so the generation keys are absent by design and only
    # the conversation itself has to be complete.
    if [[ -z $provider || -z $uuid || -z $cwd ]]; then
      printf '  Closed session %s is incomplete. Nothing changed.\n' "$label" >&2
      continue
    fi
    # A row that is not restorable has already been answered above: the list
    # carries a sentence for every one of them, so a second branch here could
    # only ever fire for a payload the projection cannot emit.
    active_target=$(active_target_for_uuid "$provider" "$uuid") || active_target=ambiguous
    case "$active_target" in
      managed:*)
        active_number=${active_target#managed:}
        # Only a pending record needs a fresh live proof. Its evidence remains
        # retained and is hidden by the live projection while this exact
        # conversation stays open.
        if [[ -z $generation || -z $old_id ]]; then
          :
        elif "$STATUS_CMD" --recovery-pending-ack \
          "$generation" "$old_id" "$uuid" >/dev/null 2>&1; then
          printf '  Confirmed session %s is open; its recovery evidence was retained.\n' \
            "$active_number"
        else
          printf '  Session %s is open, but its live proof was incomplete. Its recovery evidence was retained.\n' \
            "$active_number" >&2
        fi
        printf '  Closed session %s is already open as session %s. Opening it.\n' \
          "$label" "$active_number"
        printf '  Use fork %s from the picker to start a separate writable fork.\n' \
          "$active_number"
        QUERY=$uuid
        PAGE=1
        if build_view; then
          choose_number "$active_number"
        else
          echo "  The open session could not be shown. Nothing changed."
        fi
        continue
        ;;
      outside)
        printf '  Closed session %s is already open outside the kit. Nothing changed.\n' \
          "$label"
        continue
        ;;
      ambiguous)
        printf '  Closed session %s cannot be told apart from a live one. Nothing changed.\n' \
          "$label"
        continue
        ;;
    esac
    if launched=$("$SP_CMD" restore-exact "$provider" "$uuid" "$cwd"); then
      printf '  Restored closed session %s.\n' "$label"
      acknowledged=0
      # A row with no pending record has nothing to acknowledge; the restore
      # itself is the whole of the work.
      if [[ -z $generation || -z $old_id ]]; then
        acknowledged=1
      fi
      for attempt in {1..20}; do
        (( acknowledged == 0 )) || break
        if "$STATUS_CMD" --recovery-pending-ack \
          "$generation" "$old_id" "$uuid" >/dev/null 2>&1; then
          acknowledged=1
          break
        fi
        sleep 0.25
      done
      if (( acknowledged == 0 )); then
        printf '  Closed session %s is restored, and its record is still waiting on live proof. The record was kept.\n' "$label" >&2
      fi
    else
      printf '  Could not restore closed session %s. Its record was kept.\n' "$label" >&2
    fi
  done 3<<<"$selected"
  refresh_after_action
}
