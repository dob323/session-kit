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
from datetime import datetime
import os
import re
import shutil
import sys
import time
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
    "claude": "CLD",
    "codex": "CDX",
    "shell": "SHL",
    "unknown": "UNK",
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
PROVIDER_COLORS = {
    "claude": SESSION_PALETTE["yellow"],
    "codex": SESSION_PALETTE["cyan"],
    "shell": SESSION_PALETTE["green"],
    "unknown": SESSION_PALETTE["yellow"],
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

def shorten_aligned(text, room):
    # Detail parts are already control-sanitized. Preserve the deliberate
    # spaces that align state and time columns instead of normalizing them
    # away as ordinary prose whitespace.
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

def provider_key(item):
    return str(
        item.get("display_provider")
        or item.get("provider")
        or "unknown"
    ).casefold()

def provider_label(item):
    return provider_labels.get(provider_key(item), "UNK")

def provider_color(item):
    return PROVIDER_COLORS.get(provider_key(item), PROVIDER_COLORS["unknown"])

def account_label(item):
    if provider_key(item) not in {"claude", "codex"}:
        return ""
    return clean_display(item.get("account_alias"))[:20] or "unknown"

def age(seconds):
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"

def relative_age(timestamp_ms, now_ms):
    if (
        isinstance(timestamp_ms, bool)
        or not isinstance(timestamp_ms, int)
        or timestamp_ms < 0
    ):
        return ""
    seconds = max(0, (now_ms - timestamp_ms) // 1000)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        minutes = seconds // 60
        return f"{minutes} min ago"
    if seconds < 86400:
        hours = seconds // 3600
        return f"{hours} hr ago"
    if seconds < 30 * 86400:
        days = seconds // 86400
        return f"{days} day{'s' if days != 1 else ''} ago"
    local = datetime.fromtimestamp(timestamp_ms / 1000).astimezone()
    current = datetime.fromtimestamp(now_ms / 1000).astimezone()
    date = f"{local.strftime('%b')} {local.day}"
    return date if local.year == current.year else f"{date}, {local.year}"

def waiting_age(timestamp_ms, now_ms):
    if (
        isinstance(timestamp_ms, bool)
        or not isinstance(timestamp_ms, int)
        or timestamp_ms < 0
    ):
        return ""
    seconds = max(0, (now_ms - timestamp_ms) // 1000)
    if seconds < 60:
        return "waiting under 1 min"
    if seconds < 3600:
        return f"waiting {seconds // 60} min"
    if seconds < 86400:
        return f"waiting {seconds // 3600} hr"
    if seconds < 30 * 86400:
        days = seconds // 86400
        return f"waiting {days} day{'s' if days != 1 else ''}"
    relative = relative_age(timestamp_ms, now_ms)
    return f"waiting since {relative}"

rendered_at_ms = int(time.time() * 1000)

if query:
    match_word = "match" if len(rows) == 1 else "matches"
    session_word = "session" if source_total == 1 else "sessions"
    summary = f"{len(rows)} {match_word} of {source_total} {session_word} · {ready} ready here · {attached} open elsewhere"
    print("  " + style(shorten(summary, max(1, width - 2)), BOLD))
    search_label = "Search:"
    # The recovery screen filters by an exact conversation UUID to land on one
    # row. That is a machine's way of pointing; a person is told what it found,
    # not the identifier it used to find it.
    shown_query = (
        "one exact conversation"
        if re.fullmatch(
            r"(?:claude:|codex:)?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
            r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            query.strip(),
        )
        else query
    )
    search_value = shorten(shown_query, max(1, width - 2 - len(search_label) - 1))
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
            primary_status = "setup incomplete"
            warning_status = True
        elif row.get("needs_you"):
            primary_status = "needs your reply"
            warning_status = True
        elif row.get("reply_optional"):
            primary_status = "reply optional"
        else:
            primary_status = clean_display(
                row.get("agent_status") or "state unavailable"
            )
            if primary_status.casefold() == "state unavailable":
                primary_status = "status unavailable"
        process_age = row.get("process_age_seconds")
        has_process_age = (
            isinstance(process_age, int)
            and not isinstance(process_age, bool)
            and process_age >= 0
        )
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
        # Main rows are deliberately one line: number, title, provider, state.
        # One labelled age makes a crowded list usable without pretending a
        # session's creation time is its latest activity.
        details.append(primary_status)
        projected_now = row.get("_picker_time_as_of_unix_ms")
        if (
            isinstance(projected_now, bool)
            or not isinstance(projected_now, int)
            or projected_now < 0
        ):
            projected_now = rendered_at_ms
        age_detail = ""
        waiting_since = row.get("_picker_waiting_since_unix_ms")
        last_response = row.get("_picker_last_response_at_unix_ms")
        opened_at = row.get("started_at_unix_ms")
        if needs_attention:
            age_detail = waiting_age(waiting_since, projected_now)
        if not age_detail and row.get("provider") in {"claude", "codex"}:
            response_age = relative_age(last_response, projected_now)
            if response_age:
                age_detail = response_age
        if not age_detail:
            opened_age = relative_age(opened_at, projected_now)
            if opened_age:
                age_detail = f"opened {opened_age}"
        if age_detail:
            details.append(age_detail)
        automatic_name_state = str(
            row.get("automatic_name_state") or ""
        ).casefold()
        if automatic_name_state in {"pending", "failed"}:
            name_detail = f"name {automatic_name_state}"
            details.append(name_detail)
            if automatic_name_state == "failed":
                warning_status = True
        status_details = [primary_status]
        agents = int(
            row.get("active_subagent_count", len(row.get("subagents") or []))
        )
        if agents:
            details.append(f"{agents} subagent{'s' if agents != 1 else ''}")
        # Narrow terminals drop the subagent count first. Primary state and
        # login age remain the only fallback context, never an identifier.
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
                details,
                compact_details,
                status_details,
                needs_attention,
                warning_status,
            )
        )
    number_width = max(3, max(len(str(item[1])) for item in rendered_rows))
    prefix_width = len("      ") + number_width + len("  ")
    provider_width = max(display_width(provider_label(item[0])) for item in rendered_rows)
    account_width = max(display_width(account_label(item[0])) for item in rendered_rows)
    available_width = max(
        1, width - prefix_width - provider_width - account_width - 9
    )
    ideal_label_width = max(display_width(item[2]) for item in rendered_rows)
    state_width = max(display_width(item[5][0]) for item in rendered_rows)

    def aligned_details(parts):
        if not parts:
            return ""
        primary = parts[0]
        if len(parts) > 1:
            primary += " " * max(0, state_width - display_width(primary))
        return " | ".join((primary, *parts[1:]))

    # Optional subagent counts never take width away from titles or the primary
    # status. They are included only when the compact row already has room.
    ideal_detail_width = max(
        display_width(aligned_details(item[5])) for item in rendered_rows
    )
    if ideal_label_width + 3 + ideal_detail_width <= available_width:
        label_width = ideal_label_width
    else:
        detail_reserve = min(ideal_detail_width, max(18, available_width // 3))
        label_width = max(8, available_width - 3 - detail_reserve)

    previous_availability = None
    for row, number, title, detail_parts, compact_parts, status_parts, needs_attention, warning_status in rendered_rows:
        availability = row.get("availability")
        provider = provider_label(row)
        account = account_label(row)
        details = aligned_details(detail_parts)
        compact_details = aligned_details(compact_parts)
        status_details = aligned_details(status_parts)
        if row.get("_picker_supervisor_pin"):
            print("  " + style("Pinned", BOLD))
            previous_availability = None
        elif availability != previous_availability:
            heading = (
                "Ready to open"
                if availability == "ready"
                else "Open elsewhere"
            )
            print("  " + style(heading, BOLD))
            previous_availability = availability
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
            width
            - display_width(prefix)
            - label_width
            - provider_width
            - account_width
            - 9,
        )
        if compact_details and display_width(details) > detail_room:
            details = compact_details
        if status_details and display_width(details) > detail_room:
            details = status_details
        detail_text = shorten_aligned(details, detail_room)
        if warning_status:
            detail_text = style(detail_text, BOLD, YELLOW)
        styled_prefix = "    " + style(number_text, BOLD, GREEN) + "  "
        provider_text = style(provider, BOLD, provider_color(row))
        account_text = account + " " * max(0, account_width - display_width(account))
        print(
            styled_prefix
            + compact_label
            + " | "
            + provider_text
            + " | "
            + account_text
            + " | "
            + detail_text
        )
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

# Unresolved failed watchdog repairs. Rendering is strictly read-only; a row is
# acknowledged only by the explicit, confirmed d<n> action in Needs you.
picker_repair_failure_rows() {
  python3 - "$REPAIR_FILE" <<'PY' 2>/dev/null || true
import json
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

failed = [
    (index, item)
    for index, item in enumerate(entries)
    if isinstance(item, dict)
    and not item.get("acknowledged")
    and item.get("outcome") != "repaired"
]
if not failed:
    sys.exit(0)
print(len(failed))
for position, (index, item) in enumerate(failed, 1):
    print(f"d{position}\t{index}")
print("  Repair failures")
for position, (_index, item) in enumerate(failed, 1):
    title = " ".join(str(item.get("title") or "a session").split())[:80]
    provider = str(item.get("provider") or "unknown").title()
    print(f"    d{position}  {title} | {provider} | automatic recovery failed")
PY
}

# Unopened replies to `sp msg`, counted straight off the marker directory the
# messaging core keeps: one readdir, no subprocess of its own, nothing parsed.
# Anything unexpected at all — no directory, no permission, a count that is not
# a number — means no cue. A picker that fails to draw because a message store
# was odd would be a far worse bug than a missing envelope.
picker_message_cue_count() {
  local dir=${SESSION_KIT_MSG_UNREAD_DIR:-"$SK_STATE_DIR/messages/unread"}
  [[ -d $dir && -r $dir && -x $dir ]] || return 0
  # The subshell keeps nullglob local to the count; the picker's own globbing
  # behaviour is left exactly as it was.
  ( shopt -s nullglob; set -- "$dir"/*; printf '%s' "$#" ) 2>/dev/null
}

# The unread replies themselves, as rows a person can act on. A counter that
# says "18 new replies — run sp msg report" tells the operator there is
# something to read and then makes them go and find it; these are the
# eighteen, on the screen they are already looking at, each one selectable.
#
# Emits the machine index first, prefixed by its own length, exactly as the
# message console does — a reply's text can contain anything, including
# something that looks like a separator:
#
#   <rows>
#   r<n><TAB><msg-id>   × rows
#   <screen…>
#
# Fail-open in every direction: an unreadable store, an odd thread file, or a
# reply from a session nobody can identify costs that one row, never the
# picker.
picker_unread_reply_rows() {
  python3 - "$VIEW" "$SK_STATE_DIR" \
    "${SESSION_KIT_MSG_UNREAD_DIR:-$SK_STATE_DIR/messages/unread}" 2>/dev/null <<'REPLIES'
import json
import os
from pathlib import Path
import re
import sys
import time
import unicodedata

view_path, state_dir, unread_dir = sys.argv[1:4]
messages = Path(state_dir) / "messages"
threads = messages / "threads"
sends = messages / "sends"


def clean(value):
    text = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in str(value or "")
    )
    return " ".join(text.split())


def age(milliseconds):
    seconds = max(0, int(time.time() - milliseconds / 1000))
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m ago"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h ago"


try:
    keys = sorted(
        entry.name
        for entry in os.scandir(unread_dir)
        if entry.is_file(follow_symlinks=False)
    )
except OSError:
    raise SystemExit(0)
if not keys:
    raise SystemExit(0)

try:
    with open(view_path, encoding="utf-8") as handle:
        rows = json.load(handle).get("sessions", [])
except (OSError, ValueError):
    rows = []
live = {}
for row in rows:
    if not isinstance(row, dict):
        continue
    identity = row.get("identity") or {}
    uuid = identity.get("uuid")
    provider = row.get("provider")
    if uuid and provider in {"claude", "codex"}:
        live[f"{provider}:{uuid}"] = row

# The newest send that still names each thread, so a row opens the report the
# reply actually belongs to. Every send is searched, newest first, and the
# search stops as soon as every unread thread has one: a reply that has waited
# long enough to fall outside some recent window is exactly the reply a person
# most needs to find, and bounding the scan by recency would strand it.
wanted = set(keys)
owner = {}
try:
    records = sorted(
        (
            entry
            for entry in os.scandir(sends)
            # A regular file, opened as itself. A symlink or a directory
            # wearing a .json name is not a send record, and following one
            # would let something outside the store decide what a row opens.
            if entry.name.endswith(".json") and entry.is_file(follow_symlinks=False)
        ),
        key=lambda entry: entry.stat().st_mtime,
        reverse=True,
    )
except OSError:
    records = []
for entry in records:
    if not wanted:
        break
    # The filename stem is the only authority on which report a row opens,
    # because it is the name `sp msg report` will look for. A record whose
    # body disagrees with its own filename is malformed: opening either value
    # would be a guess, so the thread keeps no owner and its row keeps no key.
    stem = entry.name[: -len(".json")]
    if not re.fullmatch(r"[0-9a-f]{8}", stem):
        continue
    try:
        with open(entry.path, encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        continue
    if not isinstance(record, dict):
        continue
    embedded = record.get("msg_id")
    if embedded is not None and str(embedded) != stem:
        continue
    for target in record.get("targets") or ():
        if isinstance(target, dict):
            key = target.get("thread_key")
            if isinstance(key, str) and key in wanted:
                owner[key] = (stem, clean(target.get("title")))
                wanted.discard(key)

index = []
lines = []
orphans = 0
shown = 0
for key in keys:
    if shown >= 20:
        break
    provider, _, _uuid = key.partition(":")
    if provider not in {"claude", "codex"}:
        continue
    row = live.get(key)
    msg_id, remembered = owner.get(key, ("", ""))
    number = row.get("terminal_number") if isinstance(row, dict) else None
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        number = None
    title = ""
    if isinstance(row, dict):
        title = clean(row.get("display_title") or row.get("title"))
    # A reply outlives its session. One from a room that has since closed, or
    # that lives outside the manager, still has to be readable — the title the
    # send recorded is what names it once the row is gone.
    title = title or remembered or "a session that has since closed"
    try:
        replied = int((threads / f"{key}.jsonl").stat().st_mtime * 1000)
    except OSError:
        replied = 0
    where = f"#{number}" if number else "not open here"
    when = age(replied) if replied else "reply time unknown"
    shown += 1
    if not msg_id:
        # No send names this thread any more — the record was pruned, or the
        # marker outlived it. The reply is real and still worth showing, so it
        # is shown; it is not given a key it could not honour. A row that
        # looks selectable and does nothing is worse than no row.
        orphans += 1
        lines.append(
            f"     -   {where:<13} {title[:44]:<44}  {provider:<6}  "
            f"{when} (no report — s)"
        )
        continue
    position = len(index) + 1
    index.append(f"r{position}\t{msg_id}")
    lines.append(
        f"    {('r%d' % position):<4} {where:<13} {title[:44]:<44}  "
        f"{provider:<6}  {when}"
    )

if not lines:
    raise SystemExit(0)
extra = len(keys) - shown
if extra > 0:
    lines.append(f"    ({extra} more in the message centre: s)")
if orphans:
    lines.append(
        f"    ({orphans} reply thread{'s' if orphans != 1 else ''} whose message "
        "is gone — read them with s)"
    )
print(len(index))
for entry in index:
    print(entry)
print("  New replies")
for line in lines:
    print(line)
REPLIES
}

# Prompt handoffs that reached Codex but never acquired an inventory row are
# real Needs You work. The durable key is emitted only in the machine index at
# the head of this captured stream; the screen portion contains title and age.
picker_prompt_quarantine_rows() {
  local raw
  raw=$("$SP_CMD" prompt-quarantine list --json 2>/dev/null) || return 0
  [[ -n $raw ]] || return 0
  python3 - "$raw" <<'PY' 2>/dev/null
import json
import re
import sys

data = json.loads(sys.argv[1])
if not isinstance(data, dict) or data.get("schema_version") != 1:
    raise SystemExit(0)
items = data.get("items")
if not isinstance(items, list):
    raise SystemExit(0)
allowed = {
    "intake_pending": "Codex prompt intake pending",
    "outcome_unknown": "Codex prompt outcome unknown",
}

index = []
lines = []
for item in items[:20]:
    if not isinstance(item, dict):
        continue
    key = item.get("key")
    state = item.get("state")
    age = item.get("age_seconds")
    if (
        not isinstance(key, str)
        or re.fullmatch(r"[0-9a-f]{12}", key) is None
        or state not in allowed
        or isinstance(age, bool)
        or not isinstance(age, int)
        or age < 0
    ):
        continue
    if age < 120:
        age_text = f"{age}s old"
    elif age < 7200:
        age_text = f"{age // 60}m old"
    else:
        age_text = f"{age // 3600}h old"
    number = len(index) + 1
    index.append(f"q{number}\t{key}\t{state}")
    lines.append(f"    q{number:<3} {allowed[state]}  |  {age_text}")
if not index:
    raise SystemExit(0)
print(len(index))
for entry in index:
    print(entry)
print("  Needs You · prompt delivery")
for line in lines:
    print(line)
PY
}

# Session attention rows come from the same event projection as `sp msg queue`,
# but in read-only mode so opening the dashboard cannot synthesize or clear
# state. Terminal numbers are the only selectable identity shown to a person.
picker_session_attention_rows() {
  python3 - "$SNAPSHOT" "$SK_STATE_DIR" "$SCRIPT_DIR/../lib" 2>/dev/null <<'PY'
import json
from pathlib import Path
import sys
import unicodedata

snapshot_path, state_dir, library_dir = sys.argv[1:4]
with open(snapshot_path, encoding="utf-8") as handle:
    inventory = json.load(handle)
sys.path.insert(0, library_dir)
from sessionkit_events.queue import build_attention_queue

queue = build_attention_queue(inventory, Path(state_dir), mutate=False)
items = [
    item for item in queue.get("items", [])
    if isinstance(item, dict)
    and item.get("bucket") in {"needs_you", "finished_unseen"}
    and isinstance(item.get("terminal_number"), int)
    and not isinstance(item.get("terminal_number"), bool)
]
if not items:
    raise SystemExit(0)

def clean(value):
    text = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in str(value or "")
    )
    return " ".join(text.split())

print(len(items))
for item in items:
    print(item["terminal_number"])
print("  Sessions")
for item in items:
    number = item["terminal_number"]
    title = clean(item.get("title") or f"session {number}")[:70]
    provider = str(item.get("provider") or "unknown").title()
    if item.get("bucket") == "needs_you":
        waiting = item.get("waiting_ms")
        minutes = max(0, waiting // 60000) if isinstance(waiting, int) else 0
        state = f"needs your reply ({minutes}m)"
    else:
        state = "finished, not yet opened"
    print(f"    {number}  {title} | {provider} | {state}")
PY
}

# One in-process projection computes both existing counts, applies the private
# supervisor pin before pagination, and turns the first waiting queue row into
# the frozen header. Queue/store failures remain advisory and produce no cue.
picker_redraw_projection() {
  python3 - "$VIEW" "$SNAPSHOT" "$SK_STATE_DIR" \
    "$SCRIPT_DIR/../lib" "$SK_STATE_DIR/supervisor/identity" 2>/dev/null <<'PY'
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import unicodedata

view_path, snapshot_path, state_dir, library_dir, identity_path = sys.argv[1:6]
with open(view_path, encoding="utf-8") as handle:
    view = json.load(handle)

def pinned_key(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return ""
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            return ""
        raw = os.read(descriptor, 129)
    except OSError:
        return ""
    finally:
        os.close(descriptor)
    if len(raw) > 128:
        return ""
    try:
        value = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        return ""
    return value if re.fullmatch(
        r"claude:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        value,
    ) else ""

def write_view():
    directory = os.path.dirname(view_path)
    descriptor, temporary = tempfile.mkstemp(prefix=".picker-view.", dir=directory)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(view, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, view_path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass

key = pinned_key(identity_path)
rows = view.get("sessions", [])
view_changed = False
if key and isinstance(rows, list):
    provider, uuid = key.split(":", 1)
    matches = [
        index
        for index, row in enumerate(rows)
        if isinstance(row, dict)
        and row.get("provider") == provider
        and isinstance(row.get("identity"), dict)
        and row["identity"].get("uuid") == uuid
    ]
    if len(matches) == 1:
        pinned = rows.pop(matches[0])
        pinned["_picker_supervisor_pin"] = True
        rows.insert(0, pinned)
        view_changed = True

try:
    with open(snapshot_path, encoding="utf-8") as handle:
        inventory = json.load(handle)
    sys.path.insert(0, library_dir)
    from sessionkit_events.queue import build_attention_queue
    queue = build_attention_queue(inventory, Path(state_dir), mutate=False)
    needs = [
        item
        for item in queue.get("items", [])
        if isinstance(item, dict) and item.get("bucket") == "needs_you"
    ]
    finished = [
        item
        for item in queue.get("items", [])
        if isinstance(item, dict) and item.get("bucket") == "finished_unseen"
    ]
    activity = {
        item.get("thread_key"): item
        for item in queue.get("items", [])
        if isinstance(item, dict) and isinstance(item.get("thread_key"), str)
    }
    as_of = queue.get("as_of_unix_ms")
    if isinstance(rows, list) and isinstance(as_of, int) and not isinstance(as_of, bool):
        for row in rows:
            if not isinstance(row, dict):
                continue
            identity = row.get("identity")
            uuid = identity.get("uuid") if isinstance(identity, dict) else None
            provider = row.get("provider")
            item = activity.get(f"{provider}:{uuid}")
            if not isinstance(item, dict):
                continue
            row["_picker_time_as_of_unix_ms"] = as_of
            row["_picker_last_response_at_unix_ms"] = item.get(
                "last_response_at_unix_ms"
            )
            row["_picker_waiting_since_unix_ms"] = item.get(
                "waiting_since_unix_ms"
            )
            view_changed = True
except BaseException:
    needs = []
    finished = []
if view_changed:
    write_view()
print(len(view.get("outside_agents", [])))
print(int((view.get("_picker") or {}).get("unavailable_total") or 0))
print(len(needs))
print(len(finished))
PY
}

render_main() {
  local pending=$PENDING_CACHE other_count=0 unavailable_count=0
  local reply_count=0 prompt_count=0 repair_count=0
  local session_needs=0 session_finished=0 session_attention=0 attention_total=0
  local counts prompt_output repair_output extra_lines=0
  local -a count_lines=() parts=()
  MSG_REPLY_INDEX=""
  PROMPT_QUARANTINE_INDEX=""

  reply_count=$(picker_message_cue_count) || reply_count=0
  [[ $reply_count =~ ^[0-9]+$ ]] || reply_count=0
  prompt_output=$(picker_prompt_quarantine_rows) || prompt_output=""
  if [[ -n $prompt_output ]]; then
    prompt_count=${prompt_output%%$'\n'*}
  fi
  [[ $prompt_count =~ ^[0-9]+$ ]] || prompt_count=0
  repair_output=$(picker_repair_failure_rows) || repair_output=""
  if [[ -n $repair_output ]]; then
    repair_count=${repair_output%%$'\n'*}
  fi
  [[ $repair_count =~ ^[0-9]+$ ]] || repair_count=0

  counts=$(picker_redraw_projection) || counts=$'0\n0\n0\n0'
  mapfile -t count_lines <<<"$counts"
  (( ${#count_lines[@]} > 0 )) && other_count=${count_lines[0]}
  (( ${#count_lines[@]} > 1 )) && unavailable_count=${count_lines[1]}
  (( ${#count_lines[@]} > 2 )) && session_needs=${count_lines[2]}
  (( ${#count_lines[@]} > 3 )) && session_finished=${count_lines[3]}
  [[ $other_count =~ ^[0-9]+$ ]] || other_count=0
  [[ $unavailable_count =~ ^[0-9]+$ ]] || unavailable_count=0
  [[ $session_needs =~ ^[0-9]+$ ]] || session_needs=0
  [[ $session_finished =~ ^[0-9]+$ ]] || session_finished=0
  session_attention=$(( session_needs + session_finished ))
  attention_total=$(( session_attention + reply_count + prompt_count + repair_count ))
  (( attention_total == 0 )) || extra_lines=$(( extra_lines + 1 ))
  [[ -z $LIVE_WARNING ]] || extra_lines=$(( extra_lines + 1 ))

  PAGE_SIZE=$(page_size "$pending" "$extra_lines")
  # A failed sizing helper must degrade to a usable page, never to a bash
  # division-by-zero and a traceback in the login window.
  [[ $PAGE_SIZE =~ ^[0-9]+$ && $PAGE_SIZE -ge 1 ]] || PAGE_SIZE=10
  clamp_page
  render_page
  [[ -z $LIVE_WARNING ]] || printf '  Warning: %s\n' "$LIVE_WARNING"
  if (( attention_total > 0 )); then
    (( session_attention == 0 )) || parts+=("$session_attention session$([[ $session_attention == 1 ]] || printf s)")
    (( reply_count == 0 )) || parts+=("$reply_count repl$([[ $reply_count == 1 ]] && printf y || printf ies)")
    (( prompt_count == 0 )) || parts+=("$prompt_count prompt$([[ $prompt_count == 1 ]] || printf s)")
    (( repair_count == 0 )) || parts+=("$repair_count repair failure$([[ $repair_count == 1 ]] || printf s)")
    local joined="" part
    for part in "${parts[@]}"; do
      joined+="${joined:+, }$part"
    done
    printf '  Needs you: %s · %s · %s\n' "$attention_total" "$joined" "$(picker_green 'a:review')"
  fi
  echo
  local cols=${COLUMNS:-}
  [[ $cols =~ ^[0-9]+$ ]] || cols=$(tput cols 2>/dev/null || printf '80')
  [[ $cols =~ ^[0-9]+$ ]] || cols=80
  if (( cols < 60 )); then
    printf '  %s:open %s:new %s:needs %s:more %s:help\n' \
      "$(picker_green '#')" "$(picker_green n)" "$(picker_green a)" \
      "$(picker_green m)" "$(picker_green '?')"
    return
  fi
  printf '  Open %s · New %s · Needs %s (%s) · More %s · Help %s\n' \
    "$(picker_green number)" "$(picker_green n)" "$(picker_green a)" \
    "$attention_total" "$(picker_green m)" "$(picker_green '?')"
}

# One review surface for every item that can require a decision. Opening this
# view is read-only. Items clear only through their existing resolution action
# or an explicit, confirmed repair dismissal.
show_attention_menu() {
  while true; do
    local session_output="" reply_output="" prompt_output="" repair_output=""
    local session_count=0 reply_rows=0 reply_count=0 prompt_rows=0 repair_rows=0
    local session_body="" reply_body="" prompt_body="" repair_body=""
    local session_numbers="" counted=0 line answer saved_page saved_size total
    MSG_REPLY_INDEX=""
    PROMPT_QUARANTINE_INDEX=""
    REPAIR_INDEX=""

    session_output=$(picker_session_attention_rows) || session_output=""
    if [[ -n $session_output ]]; then
      session_count=${session_output%%$'\n'*}
      [[ $session_count =~ ^[0-9]+$ ]] || session_count=0
      session_body=${session_output#*$'\n'}
      counted=0
      while (( counted < session_count )); do
        line=${session_body%%$'\n'*}
        [[ $line =~ ^[0-9]+$ ]] && session_numbers+="$line"$'\n'
        session_body=${session_body#*$'\n'}
        counted=$(( counted + 1 ))
      done
    fi

    reply_count=$(picker_message_cue_count) || reply_count=0
    [[ $reply_count =~ ^[0-9]+$ ]] || reply_count=0
    reply_output=$(picker_unread_reply_rows) || reply_output=""
    if [[ -n $reply_output ]]; then
      reply_rows=${reply_output%%$'\n'*}
      [[ $reply_rows =~ ^[0-9]+$ ]] || reply_rows=0
      reply_body=${reply_output#*$'\n'}
      counted=0
      while (( counted < reply_rows )); do
        MSG_REPLY_INDEX+="${reply_body%%$'\n'*}"$'\n'
        reply_body=${reply_body#*$'\n'}
        counted=$(( counted + 1 ))
      done
    fi

    prompt_output=$(picker_prompt_quarantine_rows) || prompt_output=""
    if [[ -n $prompt_output ]]; then
      prompt_rows=${prompt_output%%$'\n'*}
      [[ $prompt_rows =~ ^[0-9]+$ ]] || prompt_rows=0
      prompt_body=${prompt_output#*$'\n'}
      counted=0
      while (( counted < prompt_rows )); do
        PROMPT_QUARANTINE_INDEX+="${prompt_body%%$'\n'*}"$'\n'
        prompt_body=${prompt_body#*$'\n'}
        counted=$(( counted + 1 ))
      done
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

    total=$(( session_count + reply_count + prompt_rows + repair_rows ))
    echo
    printf '  Needs you · %s item%s\n' "$total" "$([[ $total == 1 ]] || printf s)"
    echo
    [[ -z $session_body ]] || { printf '%s\n\n' "$session_body"; }
    [[ -z $reply_body ]] || { printf '  Replies\n%s\n\n' "${reply_body#*$'\n'}"; }
    [[ -z $prompt_body ]] || { printf '  Prompt delivery\n%s\n\n' "${prompt_body#*$'\n'}"; }
    [[ -z $repair_body ]] || { printf '%s\n\n' "$repair_body"; }
    if (( total == 0 )); then
      echo "  Nothing needs you right now."
      echo
    fi
    echo "  Open: session number · Reply: r number · Prompt: q number · Dismiss repair: d number"
    echo "  Messages: s · Enter: back"
    echo
    picker_modal_read answer "  needs ❯ " || return 0
    case "$answer" in
      "") return 0 ;;
      q|Q) picker_exit quit ;;
      r[0-9]*|R[0-9]*) open_reply_row "${answer#[rR]}" ;;
      q[0-9]*|Q[0-9]*) choose_prompt_quarantine "${answer#[qQ]}" ;;
      d[0-9]*|D[0-9]*) dismiss_repair_failure "${answer#[dD]}" ;;
      s|S) compose_message ;;
      [0-9]*)
        if [[ $answer =~ ^[0-9]+$ ]] && grep -qxF "$answer" <<<"$session_numbers"; then
          saved_page=$PAGE
          saved_size=$PAGE_SIZE
          PAGE=1
          PAGE_SIZE=$(view_item_count)
          (( PAGE_SIZE > 0 )) || PAGE_SIZE=1
          choose_number "$answer"
          PAGE=$saved_page
          PAGE_SIZE=$saved_size
        else
          echo "  Choose a session number shown here. Nothing changed."
        fi
        ;;
      *) echo "  Unknown choice. Nothing changed." ;;
    esac
  done
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
    picker_modal_read answer "  more ❯ " || return 0
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
  echo "  s             Message centre: write to sessions and watch the replies"
  echo "  r<n>          Open the report a listed new reply belongs to: r1 · r2"
  echo "  q<n>          Resolve a listed prompt delivery needing attention: q1 · q2"
  echo "  qp            Prune prompt records after their 30-day recovery window"
  echo "  v             Open the fleet supervisor"
  echo "  k <numbers>   Close displayed sessions: k 5 · k 5, 6, 8 · k 4-7 · k all"
  echo "  x <number>    Compatibility alias for k"
  echo "  /text         Search names, providers, projects, IDs, and conversation UUIDs"
  echo "  r             Refresh live state and clear search"
  echo "  o             View detectable provider roots outside the session manager (read-only)"
  echo "  u             Review exact conversation recovery; opening it changes nothing"
  echo "  name <number> Rename an exact Claude or Codex conversation"
  echo "  name reset #  Remove the local name and show the provider name"
  echo "  fork <number> Start a separate fork of an exact Claude or Codex conversation"
  echo "  action 4      Change the subscription account for an exact Claude or Codex thread"
  echo "  action 5      Apply a pending Codex bar title only when the provider is proven idle"
  echo "  next / prev   Move between pages; only visible page numbers accept actions"
  echo "  Enter / EOF   Return to a regular terminal without a session action"
  echo "  Ctrl-C        Redraw this menu (never exits; use Enter for a terminal)"
  echo
  echo "  Inside a managed session: use bye to close it, or disconnect SSH to leave it running."
  echo "  Full raw terminal history is available from an open-session action menu."
  echo
  local ignored
  picker_modal_read ignored "  Enter: Back ❯ " || return 0
}
