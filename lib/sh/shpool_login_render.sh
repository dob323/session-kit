#!/usr/bin/env bash
# Picker menu rendering: the session page, the watchdog repair banner, the main
# menu, the more menu, and help. Source this file; do not execute it.
#
# Source order: bin/shpool_login sources this module ahead of its first trap.
# These functions read REPAIR_FILE, REPAIR_BANNER, REPAIR_BANNER_LINES,
# PENDING_CACHE, PAGE, PAGE_SIZE, QUERY, VIEW, and the palette globals, all
# assigned in that file before the boot sequence runs, and call into
# lib/sh/shpool_login_theme.sh and lib/sh/shpool_login_view.sh.
#
# Globals the entry script owns are assigned there, not here.
# shellcheck disable=SC2154

render_page() {
  python3 - "$VIEW" "$PAGE" "$PAGE_SIZE" "$PICKER_STYLE" <<'PY'
import json
import os
import shutil
import sys
import unicodedata

path, page_text, size_text, style_text = sys.argv[1:5]
page, page_size = int(page_text), int(size_text)
style_enabled = style_text == "1"
with open(path, encoding="utf-8") as handle:
    data = json.load(handle)
rows = data.get("sessions", [])
picker = data.get("_picker") or {}
source_total = int(picker.get("source_total") or 0)
query = str(picker.get("query") or "")
ready = sum(row.get("availability") == "ready" for row in rows)
attached = sum(row.get("availability") == "attached" for row in rows)
item_count = len(rows)
pages = max(1, (item_count + page_size - 1) // page_size)
page = min(max(page, 1), pages)
first = (page - 1) * page_size
last = first + page_size
selected = rows[first:min(last, len(rows))]
terminal_columns = max(
    1, shutil.get_terminal_size(fallback=(100, 24)).columns
)
width = max(1, min(239, terminal_columns - 1))
# Long enough that no normal pause reaches it. Codex sessions report "running"
# for their entire life, working or waiting, so a short threshold would label
# every parked session and the warning would stop meaning anything.
stall_seconds = 2700
_raw_stall = os.environ.get("SESSION_KIT_STALL_SECONDS", "")
if _raw_stall.isdigit() and int(_raw_stall) > 0:
    stall_seconds = int(_raw_stall)
provider_labels = {
    "claude": "Claude",
    "codex": "Codex",
    "shell": "Shell",
    "unknown": "Unknown",
}
BOLD = "\033[1m"
GREEN = "\033[32m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RESET = "\033[0m"
# One palette across the kit: same codes the inventory renderer uses, and the
# exact truecolor values Codex RENDERS for the sk-<color> theme accents
# (measured from captured frames — Codex contrast-adjusts the raw theme hex),
# so a session's name is pixel-identical here, in `sp`, and in its own Codex
# status bar. Remeasure if the theme anchors ever change.
SESSION_PALETTE = {
    "red": "\033[38;2;237;93;93m",
    "blue": "\033[38;2;97;166;240m",
    "green": "\033[38;2;63;221;115m",
    "yellow": "\033[38;2;249;215;108m",
    "purple": "\033[38;2;173;115;239m",
    "orange": "\033[38;2;242;144;81m",
    "pink": "\033[38;2;240;113;177m",
    "cyan": "\033[38;2;64;216;209m",
    "lime": "\033[38;2;170;230;70m",
    "magenta": "\033[38;2;255;95;255m",
    "silver": "\033[38;2;205;210;220m",
    "sand": "\033[38;2;214;178;130m",
    "sky": "\033[38;2;150;205;255m",
    "sea": "\033[38;2;95;235;170m",
}

def style(text, *codes):
    if not style_enabled or not text:
        return text
    return "".join(codes) + text + RESET

def display_width(text):
    return sum(
        0 if unicodedata.combining(character)
        else 2 if unicodedata.east_asian_width(character) in {"W", "F"}
        else 1
        for character in text
    )

def clean_display(text):
    text = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in str(text or "")
    )
    return " ".join(text.split())

def shorten(text, room):
    text = clean_display(text)
    if display_width(text) <= room:
        return text
    kept = []
    used = 0
    for character in text:
        cells = display_width(character)
        if used + cells > max(0, room - 1):
            break
        kept.append(character)
        used += cells
    return "".join(kept) + "…"

def provider_label(item):
    provider = str(
        item.get("display_provider")
        or item.get("provider")
        or "unknown"
    ).casefold()
    return provider_labels.get(provider, "Unknown")

def age(seconds):
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"

if query:
    match_word = "match" if len(rows) == 1 else "matches"
    session_word = "session" if source_total == 1 else "sessions"
    summary = f"{len(rows)} {match_word} of {source_total} {session_word} · {ready} ready here · {attached} open elsewhere"
    print("  " + style(shorten(summary, max(1, width - 2)), BOLD))
    search_label = "Search:"
    search_value = shorten(query, max(1, width - 2 - len(search_label) - 1))
    print("  " + style(search_label, BOLD) + " " + search_value)
else:
    session_word = "session" if len(rows) == 1 else "sessions"
    summary = f"{len(rows)} {session_word} · {ready} ready here · {attached} open elsewhere"
    print("  " + style(shorten(summary, max(1, width - 2)), BOLD))
if data.get("stale"):
    source = str(data.get("source") or "").casefold()
    source_label = {
        "cache": "cached",
        "cached": "cached",
        "last-known-good": "last-known-good cached",
    }.get(source, "cached")
    warning = f"Warning: showing {source_label} inventory; actions are disabled."
    print("  " + style(shorten(warning, max(1, width - 2)), BOLD, YELLOW))
if not selected:
    print("  No matches.")
if selected:
    rendered_rows = []
    for row in selected:
        number = row.get("terminal_number")
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number <= 0
        ):
            continue
        title = row.get("display_title") or row.get("title") or ""
        details = []
        needs_attention = bool(row.get("needs_you"))
        warning_status = False
        if row.get("setup_incomplete"):
            primary_status = "! setup incomplete"
            warning_status = True
        elif row.get("needs_you"):
            primary_status = "! needs your reply"
            warning_status = True
        elif row.get("reply_optional"):
            primary_status = "reply optional"
        else:
            primary_status = clean_display(
                row.get("agent_status") or "state unavailable"
            )
            if primary_status.casefold() == "state unavailable":
                primary_status = "status unavailable"
        recent_age = row.get("recent_output_age_seconds")
        has_age = isinstance(recent_age, int) and not isinstance(recent_age, bool)
        age_text = age(max(0, recent_age)) if has_age else ""
        if not warning_status and primary_status.casefold() != "reply optional":
            if (
                primary_status.casefold() in {"running", "working"}
                and has_age
                and recent_age >= stall_seconds
            ):
                primary_status = f"quiet {age_text}"
        details.append(primary_status)
        if (
            has_age
            and 60 <= recent_age < stall_seconds
            and not primary_status.casefold().startswith("quiet ")
        ):
            details.append(f"last output {age_text}")
        automatic_name_state = str(
            row.get("automatic_name_state") or ""
        ).casefold()
        if automatic_name_state in {"pending", "failed"}:
            name_detail = f"name {automatic_name_state}"
            if needs_attention or primary_status.casefold().startswith("quiet "):
                details.append(name_detail)
            else:
                details.insert(0, name_detail)
            if automatic_name_state == "failed":
                warning_status = True
        status_details = (
            [primary_status]
            if needs_attention
            else [
                detail
                for detail in details
                if not detail.startswith("last output ")
            ]
        )
        agents = int(
            row.get("active_subagent_count", len(row.get("subagents") or []))
        )
        if agents:
            details.append(f"{agents} subagent{'s' if agents != 1 else ''}")
        # Narrow terminals drop the subagent count first and the spelled-out
        # age second. How long a session has been quiet is the one thing that
        # is never dropped.
        compact_details = [
            detail
            for detail in details
            if not detail.endswith("subagent") and not detail.endswith("subagents")
        ]
        rendered_rows.append(
            (
                row,
                number,
                title,
                " | ".join(details),
                " | ".join(compact_details),
                " | ".join(status_details),
                needs_attention,
                warning_status,
            )
        )
    number_width = max(3, max(len(str(item[1])) for item in rendered_rows))
    prefix_width = len("        ") + number_width
    available_width = max(1, width - prefix_width)
    ideal_label_width = max(display_width(item[2]) for item in rendered_rows)
    # Optional subagent counts never take width away from titles or the primary
    # status. They are included only when the compact row already has room.
    ideal_detail_width = max(display_width(item[4]) for item in rendered_rows)
    if ideal_label_width + 3 + ideal_detail_width <= available_width:
        label_width = ideal_label_width
    else:
        detail_reserve = min(ideal_detail_width, max(18, available_width // 3))
        label_width = max(8, available_width - 3 - detail_reserve)

    previous_availability = None
    previous_attention = None
    previous_provider = None
    for row, number, title, details, compact_details, status_details, needs_attention, warning_status in rendered_rows:
        availability = row.get("availability")
        provider = provider_label(row)
        if availability != previous_availability:
            heading = (
                "Ready to open"
                if availability == "ready"
                else "Open elsewhere"
            )
            print("  " + style(heading, BOLD))
            previous_availability = availability
            previous_attention = None
            previous_provider = None
        if needs_attention != previous_attention:
            previous_attention = needs_attention
            previous_provider = None
        if provider != previous_provider:
            if needs_attention:
                heading = (
                    style("Needs your reply", BOLD, YELLOW)
                    + " · "
                    + style(provider, BOLD, CYAN)
                )
            else:
                heading = style(provider, BOLD, CYAN)
            print("    " + heading)
            previous_provider = provider
        number_text = f"{number:>{number_width}}"
        prefix = f"      {number_text}  "
        compact_label = shorten(title, label_width)
        label_padding = " " * max(0, label_width - display_width(compact_label))
        # The name itself carries the session's color; padding stays unstyled
        # so column math keeps working.
        name_code = SESSION_PALETTE.get(str(row.get("display_color") or ""))
        if name_code:
            compact_label = style(compact_label, name_code)
        compact_label += label_padding
        detail_room = max(
            1,
            width - display_width(prefix) - label_width - 3,
        )
        if compact_details and display_width(details) > detail_room:
            details = compact_details
        if status_details and display_width(details) > detail_room:
            details = status_details
        detail_text = shorten(details, detail_room)
        if warning_status:
            detail_text = style(detail_text, BOLD, YELLOW)
        styled_prefix = "      " + style(number_text, BOLD, GREEN) + "  "
        print(styled_prefix + compact_label + " | " + detail_text)
if pages > 1:
    directions = []
    if page > 1:
        directions.append("prev")
    if page < pages:
        directions.append("next")
    page_label = f"Page {page}/{pages}"
    styled_directions = [style(item, BOLD, CYAN) for item in directions]
    print("  " + style(page_label, BOLD) + " | " + " | ".join(styled_directions))
PY
}

# Read once per picker run and mark as seen, so the message survives every
# redraw of this session but is not repeated at the next login.
load_repair_banner() {
  [[ -r $REPAIR_FILE ]] || return 0
  REPAIR_BANNER=$(read_repair_banner) || REPAIR_BANNER=""
  if [[ -n $REPAIR_BANNER ]]; then
    REPAIR_BANNER_LINES=$(printf '%s\n' "$REPAIR_BANNER" | wc -l)
  fi
}

show_repair_banner() {
  [[ -n $REPAIR_BANNER ]] || return 0
  printf '%s\n' "$REPAIR_BANNER"
}

read_repair_banner() {
  python3 - "$REPAIR_FILE" <<'PY' || true
import json
import os
import sys

path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    entries = data["repairs"]
    if not isinstance(entries, list):
        raise ValueError
except (OSError, ValueError, KeyError):
    sys.exit(0)

unseen = [
    item
    for item in entries
    if isinstance(item, dict) and not item.get("acknowledged")
]
if not unseen:
    sys.exit(0)

for item in unseen[-3:]:
    title = str(item.get("title") or item.get("old_shpool_id") or "a session")
    if item.get("outcome") == "repaired":
        print(f'  Recovered while you were away: "{title}" had stopped working; its conversation continues in a new session.')
    else:
        print(f'  Needs you: "{title}" stopped working and could not be recovered automatically.')
extra = len(unseen) - 3
if extra > 0:
    print(f"  ({extra} more repair{'s' if extra != 1 else ''} in the log.)")

for item in entries:
    if isinstance(item, dict):
        item["acknowledged"] = True
tmp = f"{path}.tmp.{os.getpid()}"
try:
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump({"schema_version": 1, "repairs": entries}, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)
except OSError:
    try:
        os.unlink(tmp)
    except OSError:
        pass
PY
}

render_main() {
  local pending other_count unavailable_count
  pending=$PENDING_CACHE
  # One python pass for both counts (was two spawns per redraw).
  local counts
  counts=$(python3 -c 'import json,sys
data = json.load(open(sys.argv[1]))
print(len(data.get("outside_agents", [])))
print(int((data.get("_picker") or {}).get("unavailable_total") or 0))' \
    "$VIEW" 2>/dev/null) || counts=$'0\n0'
  other_count=${counts%%$'\n'*}
  unavailable_count=${counts##*$'\n'}
  [[ $other_count =~ ^[0-9]+$ ]] || other_count=0
  [[ $unavailable_count =~ ^[0-9]+$ ]] || unavailable_count=0
  PAGE_SIZE=$(page_size "$pending" "$REPAIR_BANNER_LINES")
  # A failed sizing helper must degrade to a usable page, never to a bash
  # division-by-zero and a traceback in the login window.
  [[ $PAGE_SIZE =~ ^[0-9]+$ && $PAGE_SIZE -ge 1 ]] || PAGE_SIZE=10
  clamp_page
  render_page
  show_repair_banner
  echo
  local more_total=$(( other_count + pending + unavailable_count ))
  local cols=${COLUMNS:-}
  [[ $cols =~ ^[0-9]+$ ]] || cols=$(tput cols 2>/dev/null || printf '80')
  [[ $cols =~ ^[0-9]+$ ]] || cols=80
  if (( cols < 60 )); then
    # Narrow window: the full footers wrap, corrupting the line budget the
    # pager computed. One compact line fits down to ~34 columns; 60 columns
    # and up keep the full footer (existing rendering contract).
    if (( more_total > 0 )); then
      printf '  %s:open %s:new %s:kill %s:more %s:help\n' \
        "$(picker_green '#')" "$(picker_green n)" "$(picker_green k)" \
        "$(picker_green m)" "$(picker_green '?')"
    else
      printf '  %s:open %s:new %s:kill %s:help\n' \
        "$(picker_green '#')" "$(picker_green n)" "$(picker_green k)" \
        "$(picker_green '?')"
    fi
    return
  fi
  printf '  Open: %s · New: %s · Kill: %s number\n' \
    "$(picker_green number)" "$(picker_green n)" "$(picker_green k)"
  if (( more_total > 0 )); then
    printf '  Terminal: %s · Search: %s · More: %s (%s) · Help: %s\n' \
      "$(picker_green Enter)" "$(picker_green /text)" "$(picker_green m)" \
      "$more_total" "$(picker_green '?')"
  else
    printf '  Terminal: %s · Search: %s · Help: %s\n' \
      "$(picker_green Enter)" "$(picker_green /text)" "$(picker_green '?')"
  fi
}

# Everything that used to clutter the main screen (recovery, other-provider,
# unavailable records) lives here behind one key.
show_more_menu() {
  local pending other_count unavailable_count
  while true; do
    # Recomputed every pass: restoring recoveries from inside this menu must
    # not leave it advertising counts that are no longer true.
    refresh_pending_cache
    pending=$PENDING_CACHE
    other_count=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("outside_agents",[])))' "$VIEW" 2>/dev/null || printf '0')
    [[ $other_count =~ ^[0-9]+$ ]] || other_count=0
    unavailable_count=$(python3 -c 'import json,sys; print(int((json.load(open(sys.argv[1])).get("_picker") or {}).get("unavailable_total") or 0))' "$VIEW" 2>/dev/null || printf '0')
    [[ $unavailable_count =~ ^[0-9]+$ ]] || unavailable_count=0
    echo
    echo "  More"
    if (( other_count > 0 )); then
      printf '  %s  Other provider sessions: %s (read-only view)\n' \
        "$(picker_green o)" "$other_count"
    fi
    if (( pending > 0 )); then
      printf '  %s  Recovery: %s conversation%s to review\n' \
        "$(picker_green u)" "$pending" \
        "$([[ $pending == 1 ]] && printf '' || printf 's')"
    fi
    if (( unavailable_count > 0 )); then
      printf '     Unavailable: %s session record%s without a live shell (no actions; a reaper clears them)\n' \
        "$unavailable_count" \
        "$([[ $unavailable_count == 1 ]] && printf '' || printf 's')"
    fi
    printf '  %s  Projects: add, review, or drop the directories new sessions offer\n' \
      "$(picker_green p)"
    if (( other_count == 0 && pending == 0 && unavailable_count == 0 )); then
      echo "  Nothing else right now."
    fi
    echo "  Enter  Back"
    echo
    local answer
    picker_read answer "  more ❯ " || return 0
    case "$answer" in
      "") return 0 ;;
      o|O)
        if (( other_count > 0 )); then
          show_other_provider_sessions
        else
          echo "  Nothing to show."
        fi
        ;;
      u|U)
        # An if, not &&/||: review_recovery returning nonzero (Enter backout,
        # a failed guard that already explained itself) must not append a
        # contradictory "Nothing to review."
        if (( pending > 0 )); then
          review_recovery
        else
          echo "  Nothing to review."
        fi
        ;;
      p|P)
        show_projects_menu
        ;;
      *) echo "  Unknown choice. Use o, u, p, or Enter." ;;
    esac
  done
}

show_help() {
  echo
  echo "  Session picker help"
  echo
  echo "  number        Open an available session or show actions for one open elsewhere"
  echo "  n             Guided Claude, Codex, or managed-shell creation"
  echo "  k <numbers>   Close displayed sessions: k 5 · k 5, 6, 8 · k 4-7"
  echo "  x <number>    Compatibility alias for k"
  echo "  /text         Search names, providers, projects, IDs, and conversation UUIDs"
  echo "  r             Refresh live state and clear search"
  echo "  o             View detectable provider roots outside the session manager (read-only)"
  echo "  u             Review exact conversation recovery; opening it changes nothing"
  echo "  name <number> Rename an exact Claude or Codex conversation"
  echo "  name reset #  Remove the local name and show the provider name"
  echo "  fork <number> Start a separate fork of an exact Claude or Codex conversation"
  echo "  action 4      Apply a pending Codex bar title only when the provider is proven idle"
  echo "  next / prev   Move between pages; only visible page numbers accept actions"
  echo "  Enter / EOF   Return to a regular terminal without a session action"
  echo "  Ctrl-C        Redraw this menu (never exits; use Enter for a terminal)"
  echo
  echo "  Inside a managed session: use bye to close it, or disconnect SSH to leave it running."
  echo "  Full raw terminal history is available from an open-session action menu."
}
