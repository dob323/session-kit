#!/usr/bin/env bash
# Picker view model: the rendered row set, page sizing, and the number-to-row
# lookup every action starts from. Source this file; do not execute it.
#
# Source order: bin/shpool_login sources this module ahead of its first trap.
# These functions read SNAPSHOT, VIEW, QUERY, PAGE, PAGE_SIZE, RECOVERY,
# PICKER_GROUP_MODE, and PICKER_COMPACT, all assigned in that file before the
# boot sequence runs, and call terminal_height() from
# lib/sh/shpool_login_theme.sh and new_temp() from lib/sh/shpool_login_live.sh.
#
# Globals the entry script owns are assigned there, not here.
# shellcheck disable=SC2154

# The grouping a person asked for, or the default one. Every caller passes the
# validated value on, so an unknown mode can never reach the projection and
# reorder a list nobody asked to reorder.
picker_group_mode() {
  case "${PICKER_GROUP_MODE:-state}" in
    provider) printf 'provider' ;;
    project) printf 'project' ;;
    *) printf 'state' ;;
  esac
}

build_view() {
  local fresh
  new_temp login-view || return 1
  fresh=$NEW_TEMP
  if ! python3 - "$SNAPSHOT" "$fresh" "$QUERY" "$(picker_group_mode)" \
       "${SK_PROJECTS_FILE:-}" <<'PY'
import json
import os
import re
import sys
import unicodedata

source, destination, query, group_mode, projects_file = sys.argv[1:6]
with open(source, encoding="utf-8") as handle:
    data = json.load(handle)

if group_mode not in {"state", "provider", "project"}:
    group_mode = "state"
needle = query.casefold()
provider_order = {"claude": 0, "codex": 1, "shell": 2, "unknown": 3}
availability_order = {"ready": 0, "attached": 1}
provider_titles = {
    "claude": "Claude",
    "codex": "Codex",
    "shell": "Managed shell",
    "unknown": "Unknown",
}
NO_PROJECT = "No project"

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
        row.get("account_alias"),
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

def load_project_roots(path):
    # projects.tsv is alias<TAB>provider<TAB>absolute path. Longest root first
    # so a session opened three directories inside a project still groups with
    # the project, not with its own leaf directory.
    entries = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 3:
                    continue
                alias = clean(parts[0], 40)
                root = parts[2].strip()
                if not alias or not root.startswith("/"):
                    continue
                entries.append((os.path.normpath(root), alias))
    except OSError:
        return []
    entries.sort(key=lambda item: len(item[0]), reverse=True)
    return entries

project_roots = load_project_roots(projects_file) if projects_file else []

def project_label(row):
    # The seam project identity plugs into: an inventory row that already
    # knows its project names itself, and nothing here has to guess. Until
    # that field exists, the answer is derived from the working directory,
    # which is what the picker has always had.
    candidate = row.get("project")
    if isinstance(candidate, dict):
        for field in ("name", "alias", "slug", "title"):
            value = candidate.get(field)
            if isinstance(value, str) and value.strip():
                return clean(value, 40)
    for field in ("project_name", "project_alias", "project"):
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            return clean(value, 40)
    cwd = str(row.get("cwd") or "").strip()
    if not cwd.startswith("/"):
        return NO_PROJECT
    normalized = os.path.normpath(cwd)
    for root, alias in project_roots:
        if normalized == root or normalized.startswith(root + os.sep):
            return alias
    return clean(os.path.basename(normalized), 40) or NO_PROJECT

def provider_title(row):
    key = str(row.get("display_provider") or row.get("provider") or "").casefold()
    return provider_titles.get(key, provider_titles["unknown"])

def group_label(row):
    if group_mode == "provider":
        return provider_title(row)
    if group_mode == "project":
        return project_label(row)
    return (
        "Ready to open"
        if row.get("availability") == "ready"
        else "Open elsewhere"
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
    row["_picker_group_label"] = group_label(row)
    rows.append(row)

def default_order(row):
    return (
        availability_order.get(row.get("availability"), 9),
        not bool(row.get("needs_you")),
        provider_order.get(
            row.get("display_provider") or row.get("provider"), 9
        ),
        row.get("recent_output_at_unix_ms") is None,
        -(row.get("recent_output_at_unix_ms") or 0),
        natural_id(row.get("shpool_id_raw")),
    )

def grouped_order(row):
    # Grouping only decides which rows sit together. Inside every group the
    # order is the one the list has always used, so a person who never
    # switches grouping sees the exact list they saw before this existed.
    if group_mode == "provider":
        return (
            provider_order.get(
                row.get("display_provider") or row.get("provider"), 9
            ),
            default_order(row),
        )
    if group_mode == "project":
        label = str(row.get("_picker_group_label") or NO_PROJECT)
        return (
            1 if label == NO_PROJECT else 0,
            label.casefold(),
            default_order(row),
        )
    return (0, default_order(row))

rows.sort(key=grouped_order)
for row in rows:
    row["title"] = clean(row.get("title"), 120)
    row["display_title"] = clean(
        row.get("display_title") or row.get("title"), 120
    )
    row["display_shpool_id"] = clean(
        row.get("display_shpool_id") or row.get("shpool_id"), 96
    )
    row["native_title"] = clean(row.get("native_title"), 120)
    row["account_alias"] = clean(row.get("account_alias"), 20)

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
    "group_mode": group_mode,
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
  python3 - "$VIEW" "$height" "$pending" "$banner" \
    "${PICKER_COMPACT:-0}" <<'PY'
import json
import sys

path, height_text, pending_text, banner_text, compact_text = sys.argv[1:6]
height = int(height_text)
has_recovery = pending_text.isdigit() and int(pending_text) > 0
banner_lines = int(banner_text) if banner_text.isdigit() else 0
compact = compact_text == "1"
with open(path, encoding="utf-8") as handle:
    data = json.load(handle)
rows = data.get("sessions", [])
outside = data.get("outside_agents", [])
picker = data.get("_picker") or {}
total = len(rows)

def page_lines(first, last, paged):
    selected = rows[first:min(last, len(rows))]

    # Summary/search and stale notice. Attention and advanced counts stay on
    # one line below the session list.
    lines = 2 if picker.get("query") else 1
    if data.get("stale"):
        lines += 1

    # Group headings are what compact mode buys back. The count here and the
    # headings the renderer prints come from the same per-row label, so the
    # two can never drift into a page that scrolls its own footer away.
    previous_label = None
    for row in selected:
        label = row.get("_picker_group_label")
        if row.get("_picker_supervisor_pin"):
            lines += 1  # pinned heading
            previous_label = None
        elif not compact and label != previous_label:
            lines += 1  # group heading
            previous_label = label
        lines += 1

    if not selected:
        lines += 1
    if paged:
        lines += 1
    lines += banner_lines  # one attention cue and/or live warning
    # Spacer, compact footer, and input prompt. Compact mode drops the spacer.
    lines += 2 if compact else 3
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

# Every number the current page is showing, in display order. `all` means
# exactly what a person can see — never a session on another page.
page_numbers() {
  python3 - "$VIEW" "$PAGE" "$PAGE_SIZE" <<'PY'
import json
import sys

path, page_text, size_text = sys.argv[1:4]
page, page_size = int(page_text), int(size_text)
with open(path, encoding="utf-8") as handle:
    rows = json.load(handle).get("sessions", [])
pages = max(1, (len(rows) + page_size - 1) // page_size)
page = min(max(page, 1), pages)
first = (page - 1) * page_size
for row in rows[first : first + page_size]:
    number = row.get("terminal_number")
    if isinstance(number, int) and not isinstance(number, bool) and number > 0:
        print(number)
PY
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
