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


# Navigation stays in Bash; all visual and value decisions live beside the
# renderer used by `sp list`.  This thin adapter is the only old-picker render
# path.
render_page() {
  PYTHONPATH="$MODULE_DIR/..${PYTHONPATH:+:$PYTHONPATH}" \
    python3 - "$VIEW" "$PAGE" "$PAGE_SIZE" "$PICKER_STYLE" \
      "${PICKER_COMPACT:-0}" "${PICKER_JUMP_NUMBER:-}" <<'PY'
import json
import shutil
import sys

from sessionkit_inventory.render import render_picker_page

path, page, page_size, style, compact, jump = sys.argv[1:7]
with open(path, encoding="utf-8") as handle:
    inventory = json.load(handle)
print(
    render_picker_page(
        inventory,
        page=int(page),
        page_size=int(page_size),
        style_enabled=style == "1",
        compact=compact == "1",
        jump_number=int(jump) if jump.isdigit() else 0,
        columns=shutil.get_terminal_size(fallback=(100, 24)).columns,
    )
)
PY
}

# Unresolved watchdog records. Rendering is strictly read-only; a row is
# acknowledged only by the explicit, confirmed d number action in Needs you.
#
# Each row says what actually happened, which is not always a failed repair.
# The watchdog writes three outcomes -- `repaired`, `failed`, and `reported` --
# and `reported` means it saw something and deliberately changed nothing,
# because silence alone is never enough to act on. Every one of these rows was
# headed "automatic repair failed" regardless, so the operator's screen
# announced fifty failed repairs when not one repair had been attempted
# (2026-08-15). The heading follows the same rule.
picker_repair_failure_rows() {
  python3 - "$REPAIR_FILE" <<'PY' 2>/dev/null || true
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

path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    entries = data["repairs"]
    if not isinstance(entries, list):
        raise ValueError
except (OSError, ValueError, KeyError):
    sys.exit(0)

failed = [
    (index, item)
    for index, item in enumerate(entries)
    if isinstance(item, dict)
    and not item.get("acknowledged")
    and item.get("outcome") != "repaired"
]
if not failed:
    sys.exit(0)


def said(outcome):
    if outcome == "failed":
        return "automatic repair failed"
    if outcome == "reported":
        return "reported quiet, no repair attempted"
    return f"recorded as {outcome}" if outcome else "recorded, outcome unknown"


print(len(failed))
# Position AND identity. The position alone was a promise the file no longer
# keeps: retirement deletes records from the middle of it about once a minute,
# and `picker_modal_read` blocks for as long as a person reads the screen, so a
# `d2` typed against a drawn list landed on whatever had shifted into slot 2 --
# it acknowledged another session's warning and printed success (found in
# review, 2026-08-15). The dismissal re-checks these two fields before it
# writes, so a list that moved underneath refuses out loud instead.
for position, (index, item) in enumerate(failed, 1):
    stamp = item.get("at_unix_ms")
    stamp = str(stamp) if isinstance(stamp, int) and not isinstance(stamp, bool) else ""
    identity = " ".join(str(item.get("old_shpool_id") or "").split())
    print(f"d{position}\t{index}\t{identity}\t{stamp}")
# Name the heading after what is actually in the list rather than after the
# worst thing it could contain.
outcomes = {str(item.get("outcome") or "") for _index, item in failed}
print("  Repair failures" if outcomes == {"failed"} else "  Watchdog reports")
# Columns, not a ragged edge: pad the token and the title by terminal cells.
titles = [" ".join(str(item.get("title") or "a session").split())[:80] for _, item in failed]
title_width = max((cells(title) for title in titles), default=1)
token_width = cells(f"d{len(failed)}")
for position, (_index, item) in enumerate(failed, 1):
    title = titles[position - 1]
    provider = str(item.get("provider") or "unknown").title()
    print(f"    {pad(f'd{position}', token_width)}  {pad(title, title_width)} | {provider} | {said(str(item.get('outcome') or ''))}")
PY
}

# One in-process projection reads the counts the footer needs straight off the
# view that was just drawn. A store failure remains advisory and produces no cue.
picker_redraw_projection() {
  python3 - "$VIEW" 2>/dev/null <<'PY'
import json
import sys

view_path = sys.argv[1]
with open(view_path, encoding="utf-8") as handle:
    view = json.load(handle)
print(len(view.get("outside_agents", [])))
print(int((view.get("_picker") or {}).get("unavailable_total") or 0))
PY
}

# The sessions whose own rows say they need you. One list feeds the count on
# the home screen and the Needs you screen, so a headline can never say zero
# while three rows say otherwise -- and it is built from the whole snapshot in
# build_view, so a search filter cannot empty it either. Only the count and
# display rows leave this helper; conversation and shpool identifiers never do.
#
# A session with no number is quarantined: it is waiting and cannot be opened,
# so it is named as unavailable rather than dropped.
picker_needs_you_rows() {
  PYTHONPATH="$MODULE_DIR/..${PYTHONPATH:+:$PYTHONPATH}" \
    python3 - "$VIEW" <<'PY' 2>/dev/null || true
import json
import sys
import time
import unicodedata

from sessionkit_inventory.labels import waiting_since

def cells(text):
    return sum(
        0 if unicodedata.combining(character)
        else 2 if unicodedata.east_asian_width(character) in {"W", "F"}
        else 1
        for character in text
    )

def pad(text, width):
    return text + " " * max(0, width - cells(text))

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        picker = json.load(handle).get("_picker") or {}
except (OSError, ValueError):
    picker = {}
wanted = [item for item in (picker.get("attention") or []) if isinstance(item, dict)]
print(len(wanted))
machine_count = picker.get("machine_count", 0)
machine_needs = picker.get("machine_needs_you", 0)
machine_expandable = picker.get("machine_expandable_count", 0)
if (
    isinstance(machine_count, int)
    and not isinstance(machine_count, bool)
    and machine_count > 0
):
    if not isinstance(machine_needs, int) or isinstance(machine_needs, bool):
        machine_needs = 0
    action = "hide" if picker.get("machine_expanded") else "show"
    toggle = (
        f" | x {action}"
        if isinstance(machine_expandable, int)
        and not isinstance(machine_expandable, bool)
        and machine_expandable > 0
        else ""
    )
    print(
        f"    {machine_count} machine session{'s' if machine_count != 1 else ''}"
        f" | {machine_needs} needs you{toggle}"
    )
display_rows = []
for item in wanted:
    if item.get("subagent_orphan"):
        continue
    if item.get("origin") == "machine" and not picker.get("machine_expanded"):
        continue
    number = item.get("number") or 0
    state = str(item.get("state") or "needs you")
    label = str(number) if number else "--"
    title = str(item.get("title") or "a session")
    provider = str(item.get("provider") or "Unknown")
    since = item.get("waiting_since_unix_ms")
    if isinstance(since, int) and not isinstance(since, bool) and since > 0:
        age = waiting_since(since, int(time.time() * 1000))
        state = age if state == "needs you" else f"{state} | {age}"
    if not number:
        state = f"{state} | unavailable"
    display_rows.append((label, title, provider, state))
title_width = max((cells(title) for _label, title, _provider, _state in display_rows), default=1)
label_width = max((cells(label) for label, _title, _provider, _state in display_rows), default=1)
for label, title, provider, state in display_rows:
    print(f"    {pad(label, label_width)}  {pad(title, title_width)} | {provider} | {state}")
PY
}

render_main() {
  local pending=$PENDING_CACHE
  local extra_lines=0

  # The home screen carried a summary line here -- "needs you: N · N sessions,
  # N repair failures". It is gone by operator ruling (2026-08-15): the count it
  # advertised was dominated by watchdog records that never described a failed
  # repair, so the headline was louder than the truth and could not be trusted
  # at a glance. The `a` screen still lists every item, and it reads the same
  # helpers directly, so nothing that a person can ACT on was removed -- only
  # the number above the footer.
  #
  # Four counts were collected solely to build it (the two row helpers and the
  # two fleet flags) and are collected no longer: three interpreter starts per
  # keystroke and per five-second refresh, for a line that no longer exists.
  # picker_repair_failure_rows and picker_needs_you_rows survive because
  # show_attention_menu calls them; picker_fleet_flags had no other caller and
  # went with the line.
  [[ -z $LIVE_WARNING ]] || extra_lines=$(( extra_lines + 1 ))

  local sizing
  local -a sizing_lines=()
  sizing=$(page_size "$pending" "$extra_lines") || sizing=""
  mapfile -t sizing_lines <<<"$sizing"
  PAGE_SIZE=${sizing_lines[0]:-}
  # The row count comes back with the size, so clamp_page below reads it
  # instead of starting an interpreter to count the same rows again.
  VIEW_ITEM_COUNT=${sizing_lines[1]:-}
  [[ $VIEW_ITEM_COUNT =~ ^[0-9]+$ ]] || VIEW_ITEM_COUNT=
  # A failed sizing helper must degrade to a usable page, never to a bash
  # division-by-zero and a traceback in the login window.
  [[ $PAGE_SIZE =~ ^[0-9]+$ && $PAGE_SIZE -ge 1 ]] || PAGE_SIZE=10
  if (( ${PICKER_JUMP_PENDING:-0} )); then
    local jump_page
    if jump_page=$(page_for_number "$PICKER_JUMP_NUMBER"); then
      PAGE=$jump_page
    fi
    PICKER_JUMP_PENDING=0
  fi
  clamp_page
  render_page
  # Recorded from the same page slice that was just drawn, so a mass close can
  # be checked against what a person actually read. A repaint that changes the
  # set is remembered until the next prompt reads it.
  local drawn_numbers
  drawn_numbers=$(page_numbers)
  [[ $drawn_numbers == "$PAGE_RENDERED_NUMBERS" ]] || PAGE_RENDER_CHANGED=1
  PAGE_RENDERED_NUMBERS=$drawn_numbers
  [[ -z $LIVE_WARNING ]] || printf '  %s\n' "$LIVE_WARNING"
  # Compact mode buys its extra rows from the group headings and this spacer.
  # page_size() subtracts the same line, so the two stay in step.
  [[ ${PICKER_COMPACT:-0} == 1 ]] || echo
  local cols=${COLUMNS:-}
  if ! [[ $cols =~ ^[0-9]+$ ]]; then
    cols=$(command stty size <&2 2>/dev/null) && cols=${cols##* } || cols=
    [[ $cols =~ ^[0-9]+$ && ${cols:-0} -gt 0 ]] || {
      cols=$(tput cols 2>/dev/null) && [[ $cols =~ ^[0-9]+$ ]] || cols=
    }
    [[ $cols =~ ^[0-9]+$ ]] || cols=80
  fi
  [[ $cols =~ ^[0-9]+$ ]] || cols=80
  # One footer, one shape, at every width: verb first, lowercase, middot
  # separated. A narrow window drops items off the end; it never rewrites the
  # ones that are left in another notation.
  #
  # The footer opens by saying what Enter does HERE (operator ruling,
  # 2026-08-16): open the top row it just drew, named by its real number, or
  # start a new session when the list is empty. The number comes from the same
  # page slice the dispatcher reads, so the promise and the action agree.
  #
  # Width used to be four hand-sized tiers; the Enter hint's variable width
  # (a 1-3 digit number) broke the narrowest one the day it landed. Now every
  # segment carries its own plain-text width and the line keeps segments,
  # first to last, while they fit in cols-1, measured on the words, not the
  # color codes. The Enter hint is first, so it is the last thing a narrow
  # window ever loses.
  local enter_plain enter_colored
  if enter_plain=$(first_number_on_page); then
    enter_colored="$(picker_green '↵') open $enter_plain"
    enter_plain="↵ open $enter_plain"
  else
    enter_colored="$(picker_green '↵') new"
    enter_plain="↵ new"
  fi
  # This is the top screen, so `b` has nowhere to go but out. Where there is
  # room the footer names that door, rather than leaving the only way out of
  # the picker unnamed at every width, which it was.
  # Priority order, not reading order: the fitting loop drops from the end,
  # so position IS survival. The old tiers always kept `more` (a door to three
  # screens) at widths that dropped `history` (the rarer key); this order
  # keeps that promise.
  local -a plain=(
    "$enter_plain" "#" "kill k #" "new n" "more m"
    "needs you a" "help ?" "history h #" "leave q or b"
  )
  local -a colored=(
    "$enter_colored" "$(picker_green '#')" "kill $(picker_green 'k #')"
    "new $(picker_green n)" "more $(picker_green m)"
    "needs you $(picker_green a)" "help $(picker_green '?')"
    "history $(picker_green 'h #')"
    "leave $(picker_green q) or $(picker_green b)"
  )
  local line="  ${colored[0]}" used=$(( 2 + ${#plain[0]} )) index
  for (( index = 1; index < ${#plain[@]}; index++ )); do
    (( used + 3 + ${#plain[index]} <= cols - 1 )) || break
    line+=" · ${colored[index]}"
    used=$(( used + 3 + ${#plain[index]} ))
  done
  printf '%s\n' "$line"
}

# An unknown key is a person looking for the one that works, so every surface
# answers with its own list instead of a dead end. Callers pass the keys they
# actually dispatch -- naming a key that does nothing is the same defect in a
# politer voice.
picker_unknown_choice() {
  printf '  There is no such key on this screen. These work: %s.\n' "$1"
}

# One review surface for every item that can require a decision. Opening this
# view is read-only. Items clear only through an explicit, confirmed repair
# dismissal.
show_attention_menu() {
  while true; do
    local repair_output="" repair_rows=0 repair_body="" counted=0 answer total
    local needs_output="" needs_rows=0 needs_body=""
    REPAIR_INDEX=""

    # The same list the home count is built from, so this screen can never say
    # nothing needs you while the list behind it says three sessions do.
    needs_output=$(picker_needs_you_rows) || needs_output=""
    if [[ -n $needs_output ]]; then
      needs_rows=${needs_output%%$'\n'*}
      [[ $needs_rows =~ ^[0-9]+$ ]] || needs_rows=0
      needs_body=${needs_output#*$'\n'}
      (( needs_rows > 0 )) || needs_body=""
    fi

    repair_output=$(picker_repair_failure_rows) || repair_output=""
    if [[ -n $repair_output ]]; then
      repair_rows=${repair_output%%$'\n'*}
      [[ $repair_rows =~ ^[0-9]+$ ]] || repair_rows=0
      repair_body=${repair_output#*$'\n'}
      counted=0
      while (( counted < repair_rows )); do
        REPAIR_INDEX+="${repair_body%%$'\n'*}"$'\n'
        repair_body=${repair_body#*$'\n'}
        counted=$(( counted + 1 ))
      done
    fi

    # The two one-door signals the home line counts must render HERE too, a
    # headline that advertises a review the review cannot show is a dead end
    # (defect found in tonight's reconciliation pass).
    local fleet_body="" fleet_rows=0
    fleet_body=$(python3 - "$VIEW" 2>/dev/null <<'PY'
import glob
import json
import os
import re
import sys
import time

# Bounded and garbage-proof -- 200 records, 64 KiB each, 256 KiB stalls file --
# so one hostile or corrupt file can never hide the rest or stall an SSH login
# (audit finding, 2026-08-12). Plus sanitization:
# every printed field is fleet-controlled text headed for the login TTY, so
# control characters are stripped and lengths capped (audit finding,
# 2026-08-12), a title must never forge rows or clear the operator's screen.
#
# And the SAME de-duplication as the home count: this screen listed a session
# once as a row and again as its own stall flag, so the review said 3 where the
# headline said 1. One counted set, read from the view both surfaces share.
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        view = json.load(handle)
        _picker = view.get("_picker") or {}
except (OSError, ValueError):
    view = {}
    _picker = {}
counted = {
    str(item.get("uuid") or "")
    for item in (_picker.get("attention") or [])
    if isinstance(item, dict)
} - {""}
fleet = os.path.expanduser("~/.local/state/fleet")
lines = []
now = time.time()


def clean(value, limit):
    text = "".join(
        ch for ch in str(value or "") if ch >= " " and ch != "\x7f"
    )
    return text[:limit] or "?"


def human_text(value, limit, fallback):
    text = clean(value, 4096)
    if text == "?" and not str(value or "").strip():
        text = fallback
    text = re.sub(
        r"(?i)(?:claude:|codex:)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
        r"[0-9a-f]{4}-[0-9a-f]{12}",
        fallback,
        text,
    )
    text = re.sub(r"(?i)\bs\d{8}-\d{6}-\d+\b", fallback, text)
    text = re.sub(
        r"(?i)\b(?=[0-9a-f]{8,}\b)(?=[0-9a-f]*[a-f])(?=[0-9a-f]*[0-9])[0-9a-f]+\b",
        fallback,
        text,
    )
    return (text or fallback)[:limit]


def age(seconds):
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {seconds % 3600 // 60}m"
    return f"{seconds // 86400}d"


for path in sorted(glob.glob(fleet + "/inbox/*.json"))[:200]:
    try:
        if os.path.getsize(path) > 65536:
            continue
        with open(path, encoding="utf-8") as handle:
            rec = json.load(handle)
        if not isinstance(rec, dict) or rec.get("state") != "open":
            continue
        session = rec.get("session")
        if not isinstance(session, dict):
            session = {}
        if str(session.get("uuid") or "") in counted:
            continue
        who = human_text(session.get("title"), 48, "a session")
        waited = age(now - float(rec.get("asked_at") or now))
        header = human_text(rec.get("header") or "Decision", 60, "Decision")
        lines.append(f"  Question · {header} · {who} · needs you {waited}")
    except (OSError, ValueError, TypeError):
        continue
try:
    if os.path.getsize(fleet + "/stalls.json") <= 262144:
        with open(fleet + "/stalls.json", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and now - float(
            data.get("generated_at") or 0
        ) < 300:
            stalled = data.get("stalled")
            subjects = _picker.get("stall_subjects") or {}
            for item in (stalled if isinstance(stalled, list) else [])[:200]:
                if not isinstance(item, dict):
                    continue
                if str(item.get("key") or "") in counted:
                    continue
                try:
                    since = age(now - float(item.get("since") or now))
                except (ValueError, TypeError):
                    since = "?"
                reason = {
                    "unsurfaced": "not surfaced",
                    "unanswered": "answer overdue",
                    "silent": "response overdue",
                    "orphan": "session unavailable",
                }.get(str(item.get("reason") or "").casefold(), "attention overdue")
                session = subjects.get(str(item.get("key") or ""))
                if isinstance(session, dict):
                    who = human_text(
                        session.get("title") or "",
                        48,
                        "a session",
                    )
                    provider = clean(
                        session.get("provider") or "",
                        20,
                    ) or "Unknown"
                    subject = f"{who} · {provider}"
                else:
                    subject = "a session"
                lines.append(
                    f"  Stalled · {subject} · "
                    f"{reason} · {since}"
                )
except (OSError, ValueError, TypeError):
    pass
print(len(lines))
for line in lines:
    print(line)
PY
) || fleet_body=""
    if [[ -n $fleet_body ]]; then
      fleet_rows=${fleet_body%%$'\n'*}
      [[ $fleet_rows =~ ^[0-9]+$ ]] || fleet_rows=0
      fleet_body=${fleet_body#*$'\n'}
      [[ $fleet_body != "$fleet_rows" ]] || fleet_body=""
    fi

    total=$(( needs_rows + repair_rows + fleet_rows ))
    echo
    printf '  needs you: %s\n' "$total"
    echo
    if [[ -n $needs_body ]]; then
      echo "  Sessions"
      printf '%s\n' "$needs_body"
      # A quarantined session is listed because it is waiting, and marked
      # because there is no number to act on.
      [[ $needs_body != *' | unavailable'* ]] ||
        echo "  A session shown as -- has no shell left to open; sp help unavailable has the rest."
      echo
    fi
    [[ -z $repair_body ]] || { printf '%s\n\n' "$repair_body"; }
    [[ -z $fleet_body ]] || { printf '%s\n\n' "$fleet_body"; }
    if (( total == 0 )); then
      echo "  Nothing needs you."
      echo
    fi
    if (( fleet_rows > 0 )); then
      echo "  Answer it in its own window."
    fi
    # The one key this screen adds is offered only when it has something to
    # act on: a line naming an action with no rows is the defect this pass is
    # about, one screen further down.
    (( repair_rows == 0 )) || echo "  dismiss d number"
    echo "  ↵ back · b back · help ?"
    echo
    picker_modal_read answer "  needs ❯ " || return 0
    case "$answer" in
      ""|b|B|back|q|Q) return 0 ;;
      d[0-9]*|D[0-9]*) dismiss_repair_failure "${answer#[dD]}" ;;
      x|X) toggle_machine_sessions ;;
      \?) show_help ;;
      *)
        if [[ $needs_body == *' | x '* ]]; then
          picker_unknown_choice "d number, x, ?, b, and Enter"
        else
          picker_unknown_choice "d number, ?, b, and Enter"
        fi
        ;;
    esac
  done
}

# Everything that used to clutter the main screen (recovery, other-provider,
# unavailable records) lives here behind one key.
show_more_menu() {
  local pending other_count unavailable_count counts
  local -a count_lines=()
  while true; do
    # Recomputed every pass: restoring recoveries from inside this menu must
    # not leave it advertising counts that are no longer true.
    refresh_pending_cache
    pending=$PENDING_CACHE
    # One read of the view for both numbers, not one interpreter each.
    counts=$(picker_redraw_projection) || counts=$'0\n0'
    mapfile -t count_lines <<<"$counts"
    other_count=${count_lines[0]:-0}
    unavailable_count=${count_lines[1]:-0}
    [[ $other_count =~ ^[0-9]+$ ]] || other_count=0
    [[ $unavailable_count =~ ^[0-9]+$ ]] || unavailable_count=0
    echo
    echo "  More"
    if (( other_count > 0 )); then
      printf '  %s  Outside the kit: %s session%s (read-only)\n' \
        "$(picker_green o)" "$other_count" \
        "$([[ $other_count == 1 ]] && printf '' || printf 's')"
    fi
    if (( pending > 0 )); then
      printf '  %s  Closed sessions: %s to restore\n' \
        "$(picker_green u)" "$pending"
    fi
    if (( unavailable_count > 0 )); then
      printf '     Unavailable: %s session%s whose shell is gone. Cleanup closes them; sp help unavailable has the rest.\n' \
        "$unavailable_count" \
        "$([[ $unavailable_count == 1 ]] && printf '' || printf 's')"
    fi
    printf '  %s  Projects: add, review, or drop the directories new sessions offer\n' \
      "$(picker_green p)"
    if (( other_count == 0 && pending == 0 && unavailable_count == 0 )); then
      echo "  Outside the kit, closed sessions, and unavailable records: none."
    fi
    printf '  %s  Help: every picker key on one screen\n' "$(picker_green '?')"
    echo "  ↵ back · b back"
    echo
    local answer
    picker_modal_read answer "  more ❯ " || return 0
    case "$answer" in
      ""|b|B|back|q|Q) return 0 ;;
      o|O)
        if (( other_count > 0 )); then
          show_other_provider_sessions
        else
          echo "  Outside the kit: none."
        fi
        ;;
      u|U)
        # An if, not &&/||: review_recovery returning nonzero (Enter backout,
        # a failed guard that already explained itself) must not append a
        # contradictory "Nothing to review."
        if (( pending > 0 )); then
          review_recovery
        else
          echo "  Closed sessions: none."
        fi
        ;;
      p|P)
        show_projects_menu
        ;;
      \?) show_help ;;
      *) picker_unknown_choice "o, u, p, ?, b, and Enter" ;;
    esac
  done
}

# One key table for the whole picker. The `?` screen prints it, the footer
# advertises a subset of it, and tests/test_picker_ux.py fails when a key the
# main loop dispatches is missing from it -- which is how the help and the
# menu stopped agreeing in the first place: `a` and `m` were on the footer of
# every screen and in no help text anywhere.
#
# Emitted as section<TAB>key<TAB>meaning so one list serves both surfaces.
picker_help_rows() {
  cat <<'ROWS'
Sessions	number	Open a session, or show what can be done with one that is open elsewhere
Sessions	h number	Read a session's history without opening it
Sessions	n	New session
Sessions	k numbers	Kill sessions: k 5 · k 5, 6, 8 · k 4-7 · k all
Sessions	name number	Give a Claude or Codex conversation a name of your own
Sessions	name reset number	Drop that name and show the provider's again
Sessions	fork number	Start a separate fork of a Claude or Codex conversation
Sessions	model number	Move an idle Claude or Codex conversation to another model
Sessions	x	Show or hide machine sessions behind their counted row
Needs you	a	Everything that needs you
Needs you	g	Move to the next session that needs you, marking it with ▸
Needs you	d number	Dismiss a repair that failed, from the Needs you screen
The list	/text	Type to filter names, providers, accounts, models, and projects
The list	group	Group by state, provider, or project; group provider picks one
The list	c	Compact rows: no headings, more sessions on the screen
The list	next / prev	Move between pages; > and < do the same
The list	r	Refresh the list and clear the filter
The list	m	More: sessions outside the kit, closed sessions, projects, unavailable records
The list	p	Projects: add, review, hide, or restore the directories new sessions offer
The list	o	Show sessions outside the kit (read-only)
The list	u	Restore a closed conversation; opening this screen changes nothing
Going back	b	Back, on every screen inside the picker
Going back	Enter	Takes the most likely choice on every screen: here the top session (a new session when the list is empty), Claude Code on New session, the recommended account or project where one is named; where nothing is choosable it goes back, and the screen's own footer says which
Leaving	b	Back from the top is out: leave the picker for a regular shell
Leaving	q / quit / exit	The same, by name
Leaving	Ctrl-C	Cancel the prompt you are in; on this screen, redraw it
ROWS
}

show_help() {
  echo
  echo "  Picker help"
  local section previous="" key meaning
  while IFS=$'\t' read -r section key meaning; do
    [[ -n $key ]] || continue
    if [[ $section != "$previous" ]]; then
      echo
      printf '  %s\n' "$(picker_bold "$section")"
      previous=$section
    fi
    printf '  %-13s %s\n' "$key" "$meaning"
  done < <(picker_help_rows)
  echo
  echo "  A key takes its number with or without the space: k5 and k 5 are the same."
  echo "  Inside a session: bye closes it, and disconnecting SSH leaves it running."
  echo "  History is an action on every row."
  echo "  Grouping and compact rows last for this window; SESSION_KIT_PICKER_GROUP and"
  echo "  SESSION_KIT_PICKER_COMPACT set how it starts."
  echo
  picker_back_read "  ↵ back · b back ❯ "
}
