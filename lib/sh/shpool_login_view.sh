#!/usr/bin/env bash
# Picker view model: the rendered row set, page sizing, and the number-to-row
# lookup every action starts from. Source this file; do not execute it.
#
# Source order: bin/shpool_login sources this module ahead of its first trap.
# These functions read SNAPSHOT, VIEW, QUERY, PAGE, PAGE_SIZE, and RECOVERY,
# all assigned in that file before the boot sequence runs, and call
# terminal_height() from lib/sh/shpool_login_theme.sh and new_temp() from
# lib/sh/shpool_login_live.sh.
#
# Globals the entry script owns are assigned there, not here.
# shellcheck disable=SC2154

build_view() {
  local fresh
  new_temp login-view || return 1
  fresh=$NEW_TEMP
  if ! python3 - "$SNAPSHOT" "$fresh" "$QUERY" <<'PY'
import json
import os
import re
import sys
import unicodedata

source, destination, query = sys.argv[1:4]
with open(source, encoding="utf-8") as handle:
    data = json.load(handle)

needle = query.casefold()
provider_order = {"claude": 0, "codex": 1, "shell": 2, "unknown": 3}
availability_order = {"ready": 0, "attached": 1}

def clean(value, limit=4096):
    text = str(value or "")
    text = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in text
    )
    return " ".join(text.split())[:limit]

def matches(row):
    identity = row.get("identity") or {}
    fields = (
        row.get("shpool_id"),
        row.get("provider"),
        row.get("display_provider"),
        row.get("title"),
        row.get("display_title"),
        row.get("native_title"),
        row.get("cwd"),
        identity.get("uuid"),
        row.get("terminal_number"),
    )
    return not needle or any(needle in str(value or "").casefold() for value in fields)

def natural_id(value):
    return tuple(
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"([0-9]+)", str(value or ""))
    )

rows = []
unavailable_total = 0
for original in data.get("sessions", []):
    if not isinstance(original, dict):
        continue
    number = original.get("terminal_number")
    if (
        isinstance(number, bool)
        or not isinstance(number, int)
        or number <= 0
    ):
        unavailable_total += 1
        continue
    if not matches(original):
        continue
    row = dict(original)
    rows.append(row)

rows.sort(
    key=lambda row: (
        availability_order.get(row.get("availability"), 9),
        not bool(row.get("needs_you")),
        provider_order.get(
            row.get("display_provider") or row.get("provider"), 9
        ),
        row.get("recent_output_at_unix_ms") is None,
        -(row.get("recent_output_at_unix_ms") or 0),
        natural_id(row.get("shpool_id_raw")),
    )
)
for row in rows:
    row["title"] = clean(row.get("title"), 120)
    row["display_title"] = clean(
        row.get("display_title") or row.get("title"), 120
    )
    row["display_shpool_id"] = clean(
        row.get("display_shpool_id") or row.get("shpool_id"), 96
    )
    row["native_title"] = clean(row.get("native_title"), 120)

outside = []
for original in data.get("outside_agents", []):
    if not isinstance(original, dict) or not matches(original):
        continue
    item = dict(original)
    item["title"] = clean(item.get("title"), 120)
    item["display_title"] = clean(
        item.get("display_title") or item.get("title"), 120
    )
    item["cwd"] = clean(item.get("cwd"), 4096)
    outside.append(item)
outside.sort(
    key=lambda item: (
        provider_order.get(
            item.get("display_provider") or item.get("provider"), 9
        ),
        str(item.get("display_title") or "").casefold(),
        str((item.get("identity") or {}).get("uuid") or ""),
    )
)

projected = dict(data)
projected["sessions"] = rows
projected["outside_agents"] = outside
projected["_picker"] = {
    "query": query,
    "source_total": len(data.get("sessions", [])),
    "source_outside_total": len(data.get("outside_agents", [])),
    "unavailable_total": unavailable_total,
}
with open(destination, "w", encoding="utf-8") as handle:
    json.dump(projected, handle, sort_keys=True, separators=(",", ":"))
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(destination, 0o600)
PY
  then
    command rm -- "$fresh"
    return 1
  fi
  VIEW=$fresh
}

page_size() {
  local pending=${1:-0} banner=${2:-0} height
  height=$(terminal_height)
  python3 - "$VIEW" "$height" "$pending" "$banner" <<'PY'
import json
import sys

path, height_text, pending_text, banner_text = sys.argv[1:5]
height = int(height_text)
has_recovery = pending_text.isdigit() and int(pending_text) > 0
banner_lines = int(banner_text) if banner_text.isdigit() else 0
with open(path, encoding="utf-8") as handle:
    data = json.load(handle)
rows = data.get("sessions", [])
outside = data.get("outside_agents", [])
picker = data.get("_picker") or {}
total = len(rows)

def page_lines(first, last, paged):
    selected = rows[first:min(last, len(rows))]

    # Summary/search, optional other-provider shortcut/stale notices.
    lines = 2 if picker.get("query") else 1
    if outside:
        lines += 1
    if picker.get("unavailable_total"):
        lines += 1
    if data.get("stale"):
        lines += 1

    previous_availability = None
    previous_attention = None
    previous_provider = None
    for row in selected:
        availability = row.get("availability")
        attention = bool(row.get("needs_you"))
        provider = str(
            row.get("display_provider") or row.get("provider") or "unknown"
        )
        if availability != previous_availability:
            lines += 1  # action heading
            previous_availability = availability
            previous_attention = None
            previous_provider = None
        if attention != previous_attention:
            previous_attention = attention
            previous_provider = None
        if provider != previous_provider:
            lines += 1
            previous_provider = provider
        lines += 1

    if not selected:
        lines += 1
    if paged:
        lines += 1
    if has_recovery:
        lines += 1
    lines += banner_lines  # watchdog repair notice
    lines += 3  # spacer and two compact help lines
    lines += 1  # input prompt
    return lines

if total == 0:
    print(1)
    raise SystemExit

for size in range(total, 0, -1):
    pages = (total + size - 1) // size
    if all(
        page_lines(first, min(first + size, total), pages > 1) <= height
        for first in range(0, total, size)
    ):
        print(size)
        break
else:
    # Very short terminals still get one item and can scroll safely.
    print(1)
PY
}

view_item_count() {
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(len(d.get("sessions",[])))' "$VIEW"
}

page_count() {
  local total
  total=$(view_item_count)
  printf '%s\n' "$(( total == 0 ? 1 : (total + PAGE_SIZE - 1) / PAGE_SIZE ))"
}

clamp_page() {
  local pages
  pages=$(page_count)
  (( PAGE >= 1 )) || PAGE=1
  (( PAGE <= pages )) || PAGE=$pages
}

number_on_page() {
  local number=$1
  [[ $number =~ ^[0-9]+$ ]] || return 1
  python3 - "$VIEW" "$PAGE" "$PAGE_SIZE" "$number" <<'PY'
import json
import sys

path, page_text, size_text, wanted_text = sys.argv[1:5]
page, page_size = int(page_text), int(size_text)
wanted = int(wanted_text)
with open(path, encoding="utf-8") as handle:
    data = json.load(handle)
rows = data.get("sessions", [])
item_count = len(rows)
pages = max(1, (item_count + page_size - 1) // page_size)
page = min(max(page, 1), pages)
first = (page - 1) * page_size
last = first + page_size
selected = rows[first:min(last, len(rows))]
matches = [
    row
    for row in selected
    if row.get("terminal_number") == wanted
]
raise SystemExit(0 if len(matches) == 1 else 1)
PY
}

view_is_live() {
  python3 - "$VIEW" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
raise SystemExit(
    0
    if data.get("source") == "live" and data.get("stale") is False
    else 1
)
PY
}

require_live_actions() {
  view_is_live || {
    echo "  Live session state is unavailable. Cached rows are read-only; refresh and try again."
    echo "  Nothing changed."
    return 1
  }
}
