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
  # A new list is a new count.
  VIEW_ITEM_COUNT=
  new_temp login-view || return 1
  fresh=$NEW_TEMP
  if ! PYTHONPATH="$MODULE_DIR/..${PYTHONPATH:+:$PYTHONPATH}" \
       python3 - "$SNAPSHOT" "$fresh" "$QUERY" "$(picker_group_mode)" \
       "${SK_PROJECTS_FILE:-}" "${PICKER_MACHINE_EXPANDED:-0}" <<'PY'
import json
import os
import re
import sys
import unicodedata

from sessionkit_inventory.common import stall_threshold_seconds
from sessionkit_inventory.labels import IDLE, QUESTION, WAITING_ON_YOU, session_state
from sessionkit_inventory.model import (
    canonical_session_order_key,
    classify_top_level_sessions,
    session_is_unavailable,
)

source, destination, query, group_mode, projects_file, expanded_text = sys.argv[1:7]
with open(source, encoding="utf-8") as handle:
    data = json.load(handle)

if group_mode not in {"state", "provider", "project"}:
    group_mode = "state"
needle = query.casefold()
machine_expanded = expanded_text == "1"
provider_order = {"claude": 0, "codex": 1, "shell": 2, "unknown": 3}
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
        row.get("model"),
        row.get("title"),
        row.get("display_title"),
        row.get("native_title"),
        row.get("cwd"),
        identity.get("uuid"),
        row.get("terminal_number"),
        *(
            child.get("title")
            for child in (row.get("subagents") or [])
            if isinstance(child, dict)
        ),
    )
    return not needle or any(needle in str(value or "").casefold() for value in fields)

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
        "Ready"
        if row.get("availability") == "ready"
        else "Open elsewhere"
    )

# The one state word a row displays is the authority for whether it is waiting,
# because that word is what the person read on the list. It is derived HERE the
# same way the list derives it -- labels.session_state, the function
# render.py already calls for the state column -- rather than read from a
# precomputed field, so the two surfaces cannot answer differently and neither
# depends on a publisher having filled one in.
#
# This used to test the raw `needs_you`/`blocking_question` flags, which is a
# different question. The 2026-08-15 ruling that a finished provider turn is
# `needs you` however the vendor spells it lives in labels.STATE_WORDS and
# never reached this screen: a Codex turn ending in `task_complete` reports
# `agent_status: idle` with `needs_you: false`, so the list said `needs you`
# for it while this screen left it out and `g` skipped past it.
#
# The test is a superset of the flags it replaced -- blocking_question yields
# `question`, and needs_you yields `needs you` or `idle` -- so no row that was
# listed before can stop being listed now.
ATTENTION_STATES = {QUESTION, WAITING_ON_YOU, IDLE}
_stall_seconds = stall_threshold_seconds()


def waiting_state(row):
    """The word the list shows for this row."""

    return session_state(row, stall_seconds=_stall_seconds)


def is_waiting(row):
    """Whether this row is waiting on a person at all.

    One predicate for both the membership test and the waited-for age, because
    a row on this screen with no age is the same defect twice: an unfinished
    launch says `pending`, which is not one of the words above, and gating the
    age separately silently dropped how long it had been pending.
    """

    return waiting_state(row) in ATTENTION_STATES or bool(row.get("setup_incomplete"))


rows = []
human_rows, expandable_machine_rows, orphan_subagents = classify_top_level_sessions(
    data.get("sessions", [])
)
for item in orphan_subagents:
    item["origin"] = "machine"
    item["_picker_subagent_orphan"] = True
all_machine_rows = expandable_machine_rows + orphan_subagents
unavailable_total = sum(
    session_is_unavailable(item)
    for item in human_rows + all_machine_rows
    if isinstance(item, dict)
)
machine_rows = [item for item in expandable_machine_rows if matches(item)]
subagent_orphan_count = sum(matches(item) for item in orphan_subagents)
visible_sources = human_rows + (
    [item for item in expandable_machine_rows if matches(item)]
    if machine_expanded
    else []
)
for original in visible_sources:
    if not isinstance(original, dict):
        continue
    if session_is_unavailable(original):
        continue
    number = original.get("terminal_number")
    if not matches(original):
        continue
    row = dict(original)
    row["_picker_group_label"] = group_label(row)
    # How long this session has been waiting. The renderer has always known how
    # to say it -- and the field it reads was never written by anything, so a
    # row waiting two hours and a row waiting two minutes read identically and
    # the only number on either was its creation date. The moment a waiting
    # session went quiet IS the moment it started waiting, which is the last
    # output it produced.
    if is_waiting(row):
        waiting_since = row.get("recent_output_at_unix_ms")
        if (
            isinstance(waiting_since, int)
            and not isinstance(waiting_since, bool)
            and waiting_since > 0
        ):
            row["_picker_waiting_since_unix_ms"] = waiting_since
    rows.append(row)

def default_order(row):
    return canonical_session_order_key(row)

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

# What needs the operator, computed from the WHOLE snapshot: a search is a
# view control, and a session waiting on a person does not stop waiting
# because the list is filtered to something else. A row whose shell generation
# could not be proven has no number to act on, and it is the row most likely
# to need attention -- it is listed as unavailable rather than dropped.
attention = []
for original in sorted(
    (
        item
        for item in human_rows + all_machine_rows
        if isinstance(item, dict)
        and not session_is_unavailable(item)
        and (
            is_waiting(item)
        )
    ),
    key=default_order,
):
    number = original.get("terminal_number")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        number = 0
    attention.append(
        {
            "number": number,
            "uuid": str((original.get("identity") or {}).get("uuid") or ""),
            "title": clean(
                original.get("display_title") or original.get("title") or "a session",
                60,
            ),
            "provider": provider_title(original),
            "origin": "machine" if original.get("origin") == "machine" else "human",
            "subagent_orphan": original.get("_picker_subagent_orphan") is True,
            "state": (
                "pending"
                if original.get("setup_incomplete")
                else waiting_state(original)
            ),
            "waiting_since_unix_ms": (
                original.get("recent_output_at_unix_ms")
                if is_waiting(original)
                else None
            ),
        }
    )

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
    row["model"] = clean(row.get("model"), 80)
    row["display_model"] = clean(row.get("display_model"), 80)
    row["model_state"] = clean(row.get("model_state"), 40)

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
stall_subjects = {}
for original in human_rows:
    identity = original.get("identity") or {}
    subject = {
        "title": clean(
            original.get("display_title") or original.get("title") or "a session",
            60,
        ),
        "provider": provider_title(original),
    }
    for key in {
        str(identity.get("uuid") or ""),
        str(original.get("shpool_id_raw") or ""),
        str(original.get("shpool_id") or ""),
    } - {""}:
        stall_subjects[key] = subject
projected["_picker"] = {
    "query": query,
    "group_mode": group_mode,
    "source_total": len(human_rows) + (
        len(expandable_machine_rows) if machine_expanded else 0
    ),
    "source_outside_total": len(data.get("outside_agents", [])),
    "unavailable_total": unavailable_total,
    "attention": attention,
    "stall_subjects": stall_subjects,
    "machine_count": len(machine_rows),
    "subagent_orphan_count": subagent_orphan_count,
    "machine_expandable_count": sum(
        matches(item) for item in expandable_machine_rows
    ),
    "machine_expanded": machine_expanded,
    "machine_needs_you": sum(bool(is_waiting(item)) for item in machine_rows),
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

    # Search and stale notice. The attention summary line that used to sit
    # below the session list is gone (operator ruling, 2026-08-15), so nothing
    # here reserves a row for it.
    lines = 2 if picker.get("query") else (1 if rows else 0)
    if data.get("stale"):
        lines += 1

    # Group headings are what compact mode buys back. The count here and the
    # headings the renderer prints come from the same per-row label, so the
    # two can never drift into a page that scrolls its own footer away.
    previous_label = None
    for row in selected:
        label = row.get("_picker_group_label")
        if not compact and label != previous_label:
            lines += 1  # group heading
            previous_label = label
        lines += 1

    if first == 0 and picker.get("machine_count"):
        lines += 1

    if not selected and (
        picker.get("query")
        or not isinstance(picker.get("machine_count"), int)
        or picker.get("machine_count", 0) <= 0
    ):
        lines += 1
    if paged:
        lines += 1
    lines += banner_lines  # the live warning, when there is one
    # Spacer, compact footer, and input prompt. Compact mode drops the spacer.
    lines += 2 if compact else 3
    return lines

# The row count is printed with the size: the next caller needs exactly this
# number, and it used to start its own interpreter to count the same rows in
# the same file.
if total == 0:
    print(1)
    print(0)
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
print(total)
PY
}

view_item_count() {
  # The count the last page sizing produced, when there has been one for this
  # view. build_view clears it, so a stale number can never outlive the list it
  # counted.
  if [[ ${VIEW_ITEM_COUNT:-} =~ ^[0-9]+$ ]]; then
    printf '%s\n' "$VIEW_ITEM_COUNT"
    return 0
  fi
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(len(d.get("sessions",[])))' "$VIEW"
}

page_count() {
  local total
  total=$(view_item_count)
  [[ $total =~ ^[0-9]+$ ]] || total=0
  printf '%s\n' "$(( total == 0 ? 1 : (total + PAGE_SIZE - 1) / PAGE_SIZE ))"
}

view_index_for_number() {
  local number=$1
  [[ $number =~ ^[0-9]+$ ]] || return 1
  python3 - "$VIEW" "$number" <<'PY'
import json
import sys

path, wanted_text = sys.argv[1:3]
wanted = int(wanted_text)
with open(path, encoding="utf-8") as handle:
    rows = json.load(handle).get("sessions", [])
for index, row in enumerate(rows):
    if isinstance(row, dict) and row.get("terminal_number") == wanted:
        print(index)
        raise SystemExit(0)
raise SystemExit(1)
PY
}

page_for_number() {
  local number=$1 index
  [[ $PAGE_SIZE =~ ^[0-9]+$ && $PAGE_SIZE -ge 1 ]] || return 1
  index=$(view_index_for_number "$number") || return 1
  printf '%s\n' "$(( index / PAGE_SIZE + 1 ))"
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

# The terminal number of the top row of the page as it is currently sliced:
# the row Enter opens and the number the footer names. One source for both, so
# the promise and the action can never disagree. Empty view: status 1.
first_number_on_page() {
  python3 - "$VIEW" "$PAGE" "$PAGE_SIZE" <<'PY'
import json
import sys

path, page_text, size_text = sys.argv[1:4]
page, page_size = int(page_text), int(size_text)
with open(path, encoding="utf-8") as handle:
    data = json.load(handle)
rows = data.get("sessions", [])
if not rows:
    raise SystemExit(1)
pages = max(1, (len(rows) + page_size - 1) // page_size)
page = min(max(page, 1), pages)
number = rows[(page - 1) * page_size].get("terminal_number")
if not isinstance(number, int):
    raise SystemExit(1)
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
    echo "  Showing a cached list. Actions are off until it refreshes."
    echo "  Nothing changed."
    return 1
  }
}
