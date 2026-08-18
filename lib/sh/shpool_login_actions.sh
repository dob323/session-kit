#!/usr/bin/env bash
# Picker action menu and the proof-bound actions behind it: open, take over,
# close, rename, fork, and guided new sessions. Every state change goes out as
# a proof-bound sp command. Source this file; do not execute it.
#
# Source order: bin/shpool_login sources this module ahead of its first trap.
# These functions read SNAPSHOT, VIEW, PAGE, QUERY, RECOVERY, SP_CMD,
# PICKER_REFUSED_STATUS, and PICKER_ATTACH_FAILED_STATUS, all assigned in that
# file before the boot sequence runs, and call into the theme, live, view, and
# render modules.
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

def pad(text, room):
    return text + " " * max(0, room - cells(text))

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
print("  Outside the kit")
if data.get("stale"):
    print("  Last-known sessions outside the kit. Not confirmed live, and not openable here.")
else:
    print("  Live sessions outside the kit. Not openable here.")
if not rows:
    print("  Outside the kit: none.")
else:
    labels = {"claude": "Claude", "codex": "Codex"}
    prepared = []
    for row in rows:
        provider = labels.get(
            str(row.get("display_provider") or row.get("provider") or "").casefold(),
            "Unknown",
        )
        hint = project_hint(row.get("cwd"))
        title = safe_title(
            row.get("display_title") or row.get("title"),
            row.get("cwd"),
            provider,
        )
        detail = "not openable here"
        if hint:
            detail = f"project: {hint} | {detail}"
        prepared.append((provider, title, detail))
    prefix = "      -  "
    title_width = min(
        max((cells(title) for _provider, title, _detail in prepared), default=1),
        max(
            1,
            min(
                width - cells(prefix) - cells(detail) - 3
                for _provider, _title, detail in prepared
            ),
        ),
    )
    previous_provider = None
    for provider, title, detail in prepared:
        if provider != previous_provider:
            print(f"    {provider}")
            previous_provider = provider
        print(prefix + pad(shorten(title, title_width), title_width) + " | " + detail)
print()
PY
  picker_back_read
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

# Dismiss BY IDENTITY, never by position alone.
#
# The screen's `d` numbers were bare array positions in watchdog-repairs.json,
# captured when the list was drawn. Retirement now deletes records from the
# middle of that file on roughly every 60-second sweep, and the modal read
# blocks with no timeout for as long as a person reads the screen -- so the
# numbers on screen stopped matching the file, and `d2` acknowledged a
# different session's warning while printing "Dismissed 1 repair failure."
# (reproduced in review, 2026-08-15; the same shift was already possible when
# an append hit the 50-record cap).
#
# The row's own identity travels with its position and is re-checked against
# the file immediately before the write. A list that moved refuses out loud and
# says to look again, which is the only safe answer: the record the person
# meant is still there, just not where it was.
dismiss_repair_failure() {
  local wanted=$1 token index="" identity="" stamp=""
  [[ $wanted =~ ^[0-9]+$ ]] || return 1
  while IFS=$'\t' read -r token index identity stamp; do
    [[ $token == "d$wanted" && $index =~ ^[0-9]+$ ]] && break
    index=""
  done <<<"$REPAIR_INDEX"
  if [[ -z $index ]]; then
    printf '  There is no repair %s on this screen. Numbers shown here work.\n' "$wanted"
    return 0
  fi
  if python3 - "$REPAIR_FILE" "$index" "$identity" "$stamp" <<'PY'
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

path = Path(sys.argv[1])
index = int(sys.argv[2])
wanted_identity = sys.argv[3] if len(sys.argv) > 3 else ""
wanted_stamp = sys.argv[4] if len(sys.argv) > 4 else ""
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
# The row that was drawn, or nothing. A record that moved, was retired, or was
# replaced by another under the same position is NOT the one the person read.
stamp = item.get("at_unix_ms")
stamp = str(stamp) if isinstance(stamp, int) and not isinstance(stamp, bool) else ""
identity = " ".join(str(item.get("old_shpool_id") or "").split())
if identity != wanted_identity or stamp != wanted_stamp:
    raise SystemExit(3)
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
    echo "  Dismissed 1 repair failure."
  elif (( $? == 3 )); then
    echo "  That list changed while it was on screen, so nothing was dismissed. Open Needs you again and the numbers will match."
  else
    echo "  The repair record changed or was unsafe to update. Nothing changed."
  fi
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
  local proof status confirm_id
  proof=$(create_proof "$number") || {
    echo "  The displayed session is stale or unsafe. Nothing changed."
    return 1
  }
  TEMP_FILES+=("$proof")
  # sp's guard asks nothing; where it wants the exact session named, the
  # picker names it from the proof it just wrote, so the answer is the same
  # whether or not a terminal is attached.
  confirm_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("shpool_id") or "")' \
    "$proof" 2>/dev/null) || confirm_id=""
  SESSION_KIT_CONFIRM_ID=$confirm_id "$SP_CMD" "$command" "$proof" "$@"
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
  # on screen (marked stale, actions disabled) and the next loop retries. The
  # search, the page and the jump marker are the person's, not the snapshot's,
  # so they cross the refresh -- exactly as the background collector does it.
  if ! refresh_snapshot keep; then
    sk_log_action picker_refresh failed || true
    echo "  Live refresh failed. Showing the last confirmed list."
  fi
}

# --- Account pickers -------------------------------------------------------
#
# Both account surfaces -- the new-session wizard step and the account switch
# -- speak one vocabulary. `sp account choices` alone decides selectability and
# hands every row a `state` string ("ready", "blocked: health expired", ...).
# Nothing below re-derives, re-words, or hides it, so the row a person reads is
# the row a refusal quotes back. One implementation serves both surfaces and
# both halves of the exchange: `render` draws the list, `pick` resolves a typed
# answer, and the Enter rule is written once because they share this file.

account_choices_count() {
  python3 - "$1" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        print(len(json.load(handle).get("choices", [])))
except (OSError, ValueError):
    raise SystemExit(1)
PY
}

account_choice_ui() {
  # account_choice_ui render <choices-json> <use|switch> <title>
  # account_choice_ui pick   <choices-json> <use|switch> <typed answer>
  #
  # pick prints the chosen alias, or the sentence explaining the refusal, and
  # exits 0 chosen | 2 back | 3 refused, ask again | 1 payload unusable.
  python3 - "$@" <<'PY'
import json
import shutil
import sys
import unicodedata

action, path, mode, argument = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
try:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
except (OSError, ValueError):
    print("  The account list could not be read.")
    raise SystemExit(1)
if not isinstance(data, dict):
    print("  The account list could not be read.")
    raise SystemExit(1)
rows = [row for row in data.get("choices", []) if isinstance(row, dict)]
eligible = [row for row in rows if row.get("eligible") is True]
recommendation = str(data.get("recommendation") or "")

MISSING = "—"


def clean(value, limit=60):
    text = "".join(
        character if character.isprintable() else " " for character in str(value)
    )
    return " ".join(text.split())[:limit]


def cells(text):
    """Terminal columns, not characters: an alias is whatever a person typed."""
    return sum(
        0 if unicodedata.combining(character)
        else 2 if unicodedata.east_asian_width(character) in {"W", "F"}
        else 1
        for character in text
    )


def pad(text, width):
    return text + " " * max(0, width - cells(text))


def shorten(text, room):
    if cells(text) <= room:
        return text
    if room <= 1:
        return "…"
    kept = []
    used = 0
    for character in text:
        size = cells(character)
        if used + size > room - 1:
            break
        kept.append(character)
        used += size
    return "".join(kept) + "…"


def state_of(row):
    fallback = "ready" if row.get("eligible") is True else "blocked: state unavailable"
    return clean(row.get("state") or fallback, 80)


def usage_of(row):
    parts = []
    for label, key in (("5h", "u5h"), ("7d", "u7d")):
        value = row.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        parts.append(f"{label} {round(float(value) * 100)}%")
    return " · ".join(parts)


def identity_of(row):
    alias = clean(row.get("alias"), 40)
    email = clean(row.get("email"), 80)
    return f"{alias}: {email}" if email else alias


def number_of(alias):
    for number, row in enumerate(rows, 1):
        if row.get("alias") == alias:
            return number
    return None


# What Enter takes here. The wizard's Enter means one thing on every screen:
# the recommended choice, or the only one there is. It used to mean Back on
# this screen and "use the default project" on the next, so a person had to
# read the footer to find out which -- which is what a footer is for, but not
# what a key is for.
#
# Switching a RUNNING session's account is deliberately not in that rule: the
# person came here to name one account for one session, there is no choice to
# take on their behalf, and the footer says Enter goes back instead.
default_alias = ""
if mode == "use":
    advised = next(
        (
            row
            for row in eligible
            if recommendation and clean(row.get("alias"), 40) == recommendation
        ),
        None,
    )
    if advised is not None:
        default_alias = clean(advised.get("alias"), 40)
    elif not recommendation and len(eligible) == 1:
        default_alias = clean(eligible[0].get("alias"), 40)

if action == "render":
    print(f"  {clean(argument, 40)}")
    # Columns, not a ragged edge. The alias and the email are both variable
    # width and both sit in front of everything else, so an unpadded row made
    # the plan, the state and the usage land in a different place on every
    # line. Widths come from the rows actually on this screen.
    width = max(20, min(200, shutil.get_terminal_size(fallback=(100, 24)).columns - 1))
    cell = [
        (
            identity_of(row),
            clean(row.get("plan"), 40) or MISSING,
            state_of(row),
            usage_of(row) or MISSING,
            "recommended" if row.get("recommended") else "",
        )
        for row in rows
    ]
    identity_room = max(10, width - 46)
    identity_width = min(
        identity_room, max((cells(item[0]) for item in cell), default=1)
    )
    plan_width = max((cells(item[1]) for item in cell), default=1)
    state_width = max((cells(item[2]) for item in cell), default=1)
    usage_width = max((cells(item[3]) for item in cell), default=1)
    for number, item in enumerate(cell, 1):
        identity, plan, state, usage, mark = item
        line = (
            f"  {number:2}  {pad(shorten(identity, identity_width), identity_width)}"
            f" | {pad(plan, plan_width)} | {pad(state, state_width)}"
            f" | {pad(usage, usage_width)}"
        )
        if mark:
            line = f"{line} | {mark}"
        print(line.rstrip() if not mark else line)
    reason = clean(data.get("recommendation_reason"), 240)
    if recommendation and reason:
        print(f"  Why {clean(recommendation, 40)}: {reason}")
    roster_state = clean(
        data.get("roster_state")
        or ("fresh" if data.get("roster_fresh") else "unknown"),
        20,
    )
    if roster_state != "fresh":
        print("  Account health is unknown. Every enabled account is offered.")
    if data.get("advice_fresh") is False:
        print("  Rotation advice is stale or absent: no account is recommended.")
    blocked = data.get("recommendation_blocked")
    if isinstance(blocked, dict) and blocked.get("alias"):
        print(
            f"  {clean(blocked.get('alias'), 40)} is advised but not selectable:"
            f" {clean(blocked.get('state'), 80)}"
        )
    if rows and not eligible:
        print("  No account here is selectable; every row above says why.")
    verb = "use" if mode == "use" else "change to"
    advice_tail = ""
    if recommendation and number_of(recommendation):
        advice_tail = (
            f" ({number_of(recommendation)} is the recommended"
            f" {clean(recommendation, 40)})"
        )
    if default_alias:
        print(
            f"  ↵ {verb} {default_alias} · number  {verb} that account"
            f"{advice_tail} · b back"
        )
    else:
        print(f"  ↵ back · number  {verb} that account{advice_tail} · b back")
    raise SystemExit(0)

choice = clean(argument, 40)
if not rows:
    print("  There is no account to choose from.")
    raise SystemExit(2)
if choice.casefold() in {"b", "back"}:
    raise SystemExit(2)
if not choice:
    # Enter takes the recommended account, or the only selectable one. With
    # neither, there is nothing for it to take and it goes back -- which the
    # footer said before the person pressed it.
    if default_alias:
        print(default_alias)
        raise SystemExit(0)
    raise SystemExit(2)
if not choice.isdigit():
    print("  That is not an account number. Numbers shown here work.")
    raise SystemExit(3)
number = int(choice)
if not 1 <= number <= len(rows):
    print(f"  There is no account {number} on this screen. Numbers shown here work.")
    raise SystemExit(3)
row = rows[number - 1]
alias = clean(row.get("alias"), 40)
if row.get("eligible") is not True:
    # The refusal quotes that row's own state: the person reads why THAT
    # account is out, not a guess about a login.
    print(f"  Account {number} ({alias}) is not selectable: {state_of(row)}.")
    raise SystemExit(3)
print(alias)
PY
}

account_choices_render() {
  # $1 choices json, $2 mode, $3 title
  account_choice_ui render "$1" "$2" "$3"
}

account_choice_pick() {
  # $1 choices json, $2 mode, $3 typed answer
  account_choice_ui pick "$1" "$2" "$3"
}

change_account_number() {
  local number=$1 provider=$2 choices answer alias status count
  new_temp account-switch-choices || return
  choices=$NEW_TEMP
  if ! "$SP_CMD" account choices "$provider" >"$choices" 2>/dev/null; then
    echo "  Account choices are unavailable. Nothing changed."
    return
  fi
  count=$(account_choices_count "$choices") || {
    echo "  The account list could not be read. Nothing changed."
    return
  }
  echo
  if [[ $count == 0 ]]; then
    printf '  No %s account is enrolled, so there is nothing to change to.\n' "$provider"
    return
  fi
  account_choices_render "$choices" switch "Change to account" || {
    echo "  The account list could not be read. Nothing changed."
    return
  }
  echo
  while true; do
    picker_modal_read answer "  target account ❯ " || return
    alias=$(account_choice_pick "$choices" switch "$answer")
    status=$?
    if (( status == 0 )); then
      break
    fi
    if (( status == 3 )); then
      # A refused pick asks again on this step; the list is still on screen.
      [[ -z $alias ]] || printf '%s\n' "$alias"
      continue
    fi
    if (( status != 2 )) && [[ -n $alias ]]; then
      printf '%s\n' "$alias"
    fi
    return
  done
  if [[ ! $alias =~ ^[a-z][a-z0-9_-]{0,11}$ ]]; then
    echo "  The account list is unreadable. Nothing changed."
    return
  fi
  run_proof_action picker-account-switch "$number" "$alias" || true
  refresh_after_action
}
choose_number() {
  local number=$1 metadata
  require_live_actions || return
  number_on_page "$number" || {
    printf '  There is no session %s on this screen. Numbers shown here work.\n' "$number"
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
    # The window's tab carries the thread name for the whole attachment,
    # the only in-window name surface Codex has (Claude's own title hook
    # re-asserts the same name in-session). Cleared again on return.
    sk_tab_title "$title"
    local open_status=0
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
        printf '  Could not open session %s. It was left alone.\n' "$number"
        ;;
      *)
        printf '  Could not open session %s. It was left alone.\n' "$number"
        ;;
    esac
    sk_tab_title "session kit"
    refresh_after_action
    return
  fi

  while true; do
    echo
    printf '  %s [session %s]\n' "$title" "$number"
    echo "  Open elsewhere."
    echo
    echo "  1  Move it here (the other window disconnects)"
    echo "  2  History"
    echo "  3  Close it (the shell and everything inside will end)"
    if [[ $provider == claude || $provider == codex ]]; then
      echo "  4  Change subscription account (keeps this exact conversation)"
    fi
    if [[ $provider == codex && $title_state == pending ]]; then
      echo "  5  Apply the pending title (restarts only a proven-idle Codex provider)"
    fi
    # Enter takes the most likely option (operator ruling, 2026-08-16): a
    # person who picked a session that is open elsewhere almost always wants
    # it here. Moving it is not destruction -- the other window disconnects
    # and can take it back the same way.
    echo "  ↵ move it here · b back"
    echo
    local action offered="1, 2, 3"
    if [[ $provider == claude || $provider == codex ]]; then
      offered+=", 4"
    fi
    if [[ $provider == codex && $title_state == pending ]]; then
      offered+=", 5"
    fi
    picker_modal_read action "  action ❯ " || return 0
    case "$action" in
      b|B|back|q|Q) return ;;
      ""|1)
        run_proof_action picker-takeover "$number" || true
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
        refresh_after_action
        return
        ;;
      4)
        if [[ $provider == claude || $provider == codex ]]; then
          change_account_number "$number" "$provider"
          return
        fi
        echo "  Only a Claude or Codex conversation can change account. Nothing changed."
        ;;
      5)
        if [[ $provider == codex && $title_state == pending ]]; then
          run_proof_action picker-title-refresh "$number" || true
          refresh_after_action
          return
        fi
        echo "  That session has no pending Codex title. Nothing changed."
        ;;
      *) picker_unknown_choice "$offered, b, and Enter" ;;
    esac
  done
}

# Closing ends a shell and everything running inside it. It asks nothing: the
# number was typed against the rows on the screen, guards run before the close,
# and the conversation stays restorable afterwards.
close_row_title() {
  local number=$1 metadata
  metadata=$(number_metadata "$number") || return 1
  local -a fields=()
  mapfile -t fields <<<"$metadata"
  (( ${#fields[@]} == 8 )) || return 1
  decode64 "${fields[4]}"
}


# The one selection grammar: k 5 · k 5, 6, 8 · k 4-7 · k all. Every number is
# validated against the CURRENT page before anything closes; one bad token
# refuses the whole request so a typo never kills a neighbor.
#
# `all` means the page that was drawn, not the page that exists now. A live
# refresh reorders the list on its own -- the sort follows recent output and
# needs-you -- so resolving `all` at action time could close a set the person
# had never seen. The drawn set is closed, and only if it is still the set on
# screen; anything else refuses and repaints.
direct_close() {
  local raw=$1 token
  local -a numbers=()
  if [[ ${raw,,} == a || ${raw,,} == all ]]; then
    local -a shown=() current=()
    mapfile -t shown <<<"$PAGE_RENDERED_NUMBERS"
    mapfile -t current < <(page_numbers)
    local drawn_set="" live_set=""
    for token in "${shown[@]}"; do
      [[ -z $token ]] || drawn_set+="$token"$'\n'
    done
    for token in "${current[@]}"; do
      [[ -z $token ]] || live_set+="$token"$'\n'
    done
    [[ -n $drawn_set ]] || {
      echo "  There is nothing on this screen to close. Nothing changed."
      return
    }
    if (( PAGE_RENDER_CHANGED )) || [[ $drawn_set != "$live_set" ]]; then
      echo "  The list changed. Nothing changed."
      return
    fi
    mapfile -t numbers <<<"${drawn_set%$'\n'}"
  else
  raw=${raw//,/ }
  for token in $raw; do
    if [[ $token =~ ^[0-9]+$ ]]; then
      numbers+=("$token")
    elif [[ $token =~ ^([0-9]+)-([0-9]+)$ ]]; then
      local first=${BASH_REMATCH[1]} last=${BASH_REMATCH[2]}
      if (( first > last || last - first > 98 )); then
        echo "  There is no such range. Numbers shown here work."
        return
      fi
      local expanded
      for (( expanded=first; expanded<=last; expanded++ )); do
        numbers+=("$expanded")
      done
    else
      echo "  There is no session number in that. Numbers shown here work."
      return
    fi
  done
  fi
  (( ${#numbers[@]} > 0 )) || {
    echo "  There is no session number in that. Numbers shown here work."
    return
  }
  require_live_actions || return
  local number
  for number in "${numbers[@]}"; do
    number_on_page "$number" || {
      printf '  There is no session %s on this screen. Numbers shown here work.\n' "$number"
      return
    }
  done
  local -a closed=()
  for number in "${numbers[@]}"; do
    if run_proof_action picker-close "$number"; then
      closed+=("$number")
    fi
  done
  # Say what happened, here, before the refresh. The only feedback used to be a
  # list redrawn without that row -- so when the refresh failed, the screen
  # showed "Showing the last confirmed list" with the closed session still on
  # it and nothing anywhere saying it had been closed.
  if (( ${#closed[@]} == 1 )); then
    printf '  Closed session %s.\n' "${closed[0]}"
  elif (( ${#closed[@]} > 1 )); then
    local joined="" item
    for item in "${closed[@]}"; do
      joined+="${joined:+, }$item"
    done
    printf '  Closed %s sessions: %s.\n' "${#closed[@]}" "$joined"
  fi
  refresh_after_action
}

# T11: history took any number in the whole view while every other row key
# refuses one that is not on the screen. One typed number, one meaning.
history_number() {
  local number=${1//[[:space:]]/}
  if [[ ! $number =~ ^[0-9]+$ ]]; then
    echo "  That needs a session number, such as h 19. Numbers shown here work."
    return
  fi
  number_on_page "$number" || {
    printf '  There is no session %s on this screen. Numbers shown here work.\n' "$number"
    return
  }
  run_proof_action picker-history "$number" || true
}

rename_number() {
  local number=$1 metadata provider
  require_live_actions || return
  number_on_page "$number" || {
    printf '  There is no session %s on this screen. Numbers shown here work.\n' "$number"
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
  picker_modal_read title "  New name (↵ back) ❯ " || return 0
  [[ -n ${title//[[:space:]]/} ]] || {
    echo "  Nothing changed."
    return
  }
  run_proof_action picker-name "$number" "$title" || true
  refresh_after_action
}

change_model_number() {
  local number=$1 metadata provider requested choices
  require_live_actions || return
  number_on_page "$number" || {
    printf '  There is no session %s on this screen. Numbers shown here work.\n' "$number"
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
    echo "  Only a Claude or Codex conversation can change model."
    return
  fi
  choices=$(PYTHONPATH="$MODULE_DIR/..${PYTHONPATH:+:$PYTHONPATH}" \
    python3 - "$provider" <<'PY'
import os
import sys
from sessionkit_inventory.worker_model import configured_models

for model in configured_models(sys.argv[1], os.environ):
    print(model)
PY
) || choices=""
  if [[ -z $choices ]]; then
    echo "  No models are configured for this provider. Nothing changed."
    return
  fi
  echo
  echo "  Models"
  local model index=0
  while IFS= read -r model; do
    index=$(( index + 1 ))
    printf '    %s  %s\n' "$index" "$model"
  done <<<"$choices"
  picker_modal_read requested "  Model number or exact name · ↵ back · b back ❯ " || return 0
  [[ -n ${requested//[[:space:]]/} ]] || {
    echo "  Nothing changed."
    return
  }
  # `b` is Back here rather than a model name: no provider names a model `b`,
  # and the validator below would refuse it anyway with a longer sentence.
  case "${requested,,}" in
    b|back) return ;;
  esac
  if [[ $requested =~ ^[0-9]+$ ]]; then
    local selected=""
    index=0
    while IFS= read -r model; do
      index=$(( index + 1 ))
      if [[ $requested == "$index" ]]; then
        selected=$model
        break
      fi
    done <<<"$choices"
    requested=$selected
  elif ! grep -Fqx -- "$requested" <<<"$choices"; then
    requested=""
  fi
  [[ -n $requested ]] || {
    echo "  That model is not in this provider's configured list. Nothing changed."
    return
  }
  run_proof_action picker-change-model "$number" "$requested" || true
  refresh_after_action
}

reset_name_number() {
  local number=$1 metadata provider uuid confidence
  require_live_actions || return
  number_on_page "$number" || {
    printf '  There is no session %s on this screen. Numbers shown here work.\n' "$number"
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
    printf '  There is no session %s on this screen. Numbers shown here work.\n' "$number"
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

# ---- jump and view toggles ---------------------------------------------
# Every session the screen's estate-wide attention count includes. Hidden
# machine sessions carry only their origin here; their titles never leave the
# private view until the explicit show action expands them.
picker_attention_numbers() {
  python3 - "$VIEW" 2>/dev/null <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        data = json.load(handle)
except (OSError, ValueError):
    raise SystemExit(0)
picker = data.get("_picker") or {}
for row in picker.get("attention") or []:
    if not isinstance(row, dict):
        continue
    number = row.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        continue
    print(f"{number}\t{row.get('origin') or 'human'}")
PY
}

# Move to the next session that wants a person, wrapping at the end. The row
# is marked rather than selected: nothing is opened, attached, or acknowledged
# by finding it.
jump_next_attention() {
  local line number origin chosen="" chosen_origin=human first="" first_origin=human
  local previous_query=$QUERY previous_expanded=${PICKER_MACHINE_EXPANDED:-0}
  local seen_current=0
  while IFS=$'\t' read -r number origin; do
    [[ $number =~ ^[0-9]+$ ]] || continue
    if [[ -z $first ]]; then
      first=$number
      first_origin=$origin
    fi
    if [[ -z $PICKER_JUMP_NUMBER ]]; then
      chosen=$number
      chosen_origin=$origin
      break
    fi
    if (( seen_current )) && [[ -z $chosen ]]; then
      chosen=$number
      chosen_origin=$origin
      break
    fi
    [[ $number == "$PICKER_JUMP_NUMBER" ]] && seen_current=1
  done < <(picker_attention_numbers)
  if [[ -z $chosen ]]; then
    if [[ -z $first ]]; then
      PICKER_JUMP_NUMBER=""
      echo "  Nothing needs you."
      return
    fi
    # Past the last one, or the marked row is gone: start again at the top.
    chosen=$first
    chosen_origin=$first_origin
  fi
  if [[ $chosen_origin == machine ]]; then
    PICKER_MACHINE_EXPANDED=1
  fi
  # First try the person's current search, expanding machine rows when the
  # chosen session needs it. Clear the search only when it actually hides the
  # target, and say so when that happens.
  if [[ ${PICKER_MACHINE_EXPANDED:-0} != "$previous_expanded" ]]; then
    build_view || {
      PICKER_MACHINE_EXPANDED=$previous_expanded
      build_view || true
      echo "  The list could not show that session. Nothing changed."
      return
    }
  fi
  local chosen_index
  chosen_index=$(view_index_for_number "$chosen") || chosen_index=""
  if [[ -z $chosen_index && -n $QUERY ]]; then
    QUERY=""
    if ! build_view; then
      QUERY=$previous_query
      PICKER_MACHINE_EXPANDED=$previous_expanded
      build_view || true
      echo "  The list could not show that session. Nothing changed."
      return
    fi
    chosen_index=$(view_index_for_number "$chosen") || chosen_index=""
    [[ -z $chosen_index ]] || printf '  Search cleared to show session %s.\n' "$(picker_green "$chosen")"
  fi
  if [[ -z $chosen_index ]]; then
    QUERY=$previous_query
    PICKER_MACHINE_EXPANDED=$previous_expanded
    build_view || true
    echo "  The list could not show that session. Nothing changed."
    return
  fi
  PICKER_JUMP_NUMBER=$chosen
  PICKER_JUMP_PENDING=1
  printf '  Session %s needs you.\n' "$(picker_green "$chosen")"
}

toggle_machine_sessions() {
  local previous=${PICKER_MACHINE_EXPANDED:-0}
  local count counts subagent_total
  counts=$(python3 - "$VIEW" <<'PY' 2>/dev/null
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    picker = json.load(handle).get("_picker") or {}
for name in ("machine_expandable_count", "subagent_orphan_count"):
    value = picker.get(name, 0)
    print(value if isinstance(value, int) and not isinstance(value, bool) else 0)
PY
) || counts=$'0\n0'
  count=${counts%%$'\n'*}
  subagent_total=${counts#*$'\n'}
  if [[ ! $count =~ ^[0-9]+$ || $count -eq 0 ]]; then
    if [[ $subagent_total =~ ^[0-9]+$ && $subagent_total -gt 0 ]]; then
      echo "  Subagent sessions stay with their parent. Nothing changed."
      return
    fi
    echo "  No machine sessions are on this screen. Nothing changed."
    return
  fi
  if [[ $previous == 1 ]]; then
    PICKER_MACHINE_EXPANDED=0
    echo "  Machine sessions hidden."
  else
    PICKER_MACHINE_EXPANDED=1
    echo "  Machine sessions shown."
  fi
  PICKER_JUMP_NUMBER=""
  PICKER_JUMP_PENDING=0
  PAGE=1
  if ! build_view; then
    PICKER_MACHINE_EXPANDED=$previous
    build_view || true
    echo "  The machine-session view could not change."
  fi
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
      echo "  Live refresh failed. Showing the last confirmed list."
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
# The provider is already chosen before this list is shown, and a project's
# own row records a default at most, never a lock. Two rows that differ only
# by provider are one directory listed twice, so keep the first and show it
# under its configured alias.
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
            and configured_provider in {"claude", "codex", "shell", "any"}
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
  local provider=$1 choices answer selected status count
  GUIDED_ACCOUNT=
  new_temp login-accounts || return 1
  choices=$NEW_TEMP
  if ! "$SP_CMD" account choices "$provider" >"$choices" 2>/dev/null; then
    echo "  Account choices are unavailable. Nothing changed."
    return 1
  fi
  count=$(account_choices_count "$choices") || {
    echo "  The account list is unreadable. Nothing changed."
    return 1
  }
  echo
  # The enrolment instruction replaces the list. It used to trail an empty
  # list and a footer offering choices that did not exist.
  if [[ $count == 0 ]]; then
    printf '  No %s account is enrolled. Use sp account adopt-default or sp account enroll first.\n' "$provider"
    return 1
  fi
  account_choices_render "$choices" use "Account" || {
    echo "  The account list is unreadable. Nothing changed."
    return 1
  }
  echo
  while true; do
    picker_modal_read answer "  account ❯ " || return 1
    selected=$(account_choice_pick "$choices" use "$answer")
    status=$?
    if (( status == 0 )); then
      break
    fi
    if (( status == 3 )); then
      # A refused pick asks again here. Unwinding to the dashboard threw away
      # the provider the person had already chosen.
      [[ -z $selected ]] || printf '%s\n' "$selected"
      continue
    fi
    if (( status != 2 )) && [[ -n $selected ]]; then
      printf '%s\n' "$selected"
    fi
    return 1
  done
  if [[ ! $selected =~ ^[a-z][a-z0-9_-]{0,11}$ ]]; then
    # This abort was silent, which read as the picker ignoring a keypress.
    echo "  The account list is unreadable. Nothing changed."
    return 1
  fi
  GUIDED_ACCOUNT=$selected
}
guided_new() {
  require_live_actions || return
  echo
  echo "  New session"
  echo "  1  Claude Code"
  echo "  2  Codex"
  echo "  3  Shell"
  # Enter takes the most likely option on every screen (operator ruling,
  # 2026-08-16): here that is Claude Code. Enter-as-back on this screen sent
  # the most common act in the picker -- opening a new thread -- backwards.
  echo "  ↵ Claude Code · b back"
  echo
  local answer provider
  picker_modal_read answer "  provider ❯ " || return 0
  case "$answer" in
    b|B|back|q|Q) return ;;
    ""|1) provider=claude ;;
    2) provider=codex ;;
    3) provider=shell ;;
    *)
      picker_unknown_choice "1, 2, 3, Enter, and b"
      return
      ;;
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
    echo "  The project list is unavailable. Nothing changed."
    return
  }
  echo
  python3 - "$projects" <<'PY'
import json,sys,unicodedata
d=json.load(open(sys.argv[1]))
default=d.get("default")
print("  Project")
def cells(text):
    return sum(
        0 if unicodedata.combining(character)
        else 2 if unicodedata.east_asian_width(character) in {"W", "F"}
        else 1
        for character in text
    )
# The alias is variable width and the path sits behind it: one column, so the
# paths read as a list instead of a staircase.
alias_width=max((cells(row["alias"]) for row in d.get("projects",[])), default=0)
for number,row in enumerate(d.get("projects",[]),1):
    label=row["alias"] + ":"
    label += " " * max(0, alias_width + 1 - cells(label))
    print(f"  {number:2}  {label} {row['cwd']}")
if default:
    row=d["projects"][default-1]
    print(f"  ↵ use {row['alias']}: {row['cwd']} · number  use that project · b back")
else:
    print("  ↵ use the current directory · number  use that project · b back")
PY
  echo
  local project_choice project_alias
  picker_modal_read project_choice "  project ❯ " || return 0
  case "${project_choice,,}" in
    b|back) return ;;
  esac
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
    echo "  There is no such project on this screen. Numbers shown here work."
    return
  }
  if [[ $provider == claude || $provider == codex ]]; then
    echo "  Name it later with name number."
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
