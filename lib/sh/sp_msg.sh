#!/usr/bin/env bash
# sp msg: the operator's message surface over the messaging core. Every verb
# here is a thin, auditable wrapper — target resolution, delivery, the ledger,
# and unread state all live in `python3 $INVENTORY_CORE msg ...`. This file
# only parses the command line, asks for the one confirmation a mass send
# needs, and renders what the core returns. Source this file; do not execute
# it.
#
# Source order: bin/sp sources this module ahead of its first trap, after
# SCRIPT_DIR, session_kit_common, and INVENTORY_CORE are in place. Nothing
# here takes the create lock or touches a session: sending never attaches,
# restarts, or writes to another terminal.
#
# Rendering is deliberately split from fetching (msg_fetch_report /
# msg_render_report), so the live console below polls the same core verb and
# reuses the same renderer without a second copy of the layout. `sp msg
# report` is that console whenever it has a terminal to draw on, and the
# static render everywhere else — a pipe, a cron, SESSION_KIT_NONINTERACTIVE,
# a dumb terminal, or SESSION_KIT_MSG_CONSOLE=0.
#
# Globals the entry script owns are assigned there, not here.
# shellcheck disable=SC2154

msg_usage() {
  cat <<'EOF'
  sp msg <all|idle|3|1,3,5|2-4|shpool-id> "text" [--fyi] [--yes]
  sp msg report [msg-id]
  sp msg reply <msg-id> "text"
  sp msg list
EOF
}

msg_command() {
  local subcommand=${1:-}
  case "$subcommand" in
    '')
      # On a terminal, no arguments is the whole feature: write a message,
      # watch the answers come back, write another. The usage text is for
      # everything that cannot show a screen.
      if msg_console_enabled; then
        msg_console ""
        return
      fi
      msg_usage >&2
      return 2
      ;;
    help | -h | --help)
      msg_usage
      return 0
      ;;
    report)
      shift
      msg_report "$@"
      ;;
    queue)
      # The attention queue lives on the inventory core. Without this arm the
      # word would fall through to the sender and address a session named
      # "queue" — a trap, not a message.
      shift
      python3 "$INVENTORY_CORE" msg queue "$@"
      ;;
    intake)
      # The intake spool is supervisor state on the inventory core, and its
      # verbs are machine JSON. Same trap as `queue`: without this arm the
      # word falls through to the sender and addresses a session named
      # "intake".
      shift
      python3 "$INVENTORY_CORE" msg intake "$@"
      ;;
    reply)
      shift
      msg_reply "$@"
      ;;
    list)
      shift
      msg_list "$@"
      ;;
    *)
      msg_send_command "$@"
      ;;
  esac
}

# The renderer is one program with a mode argument so every table, every
# truncation, and — most of all — the control-character scrub sits in exactly
# one place. Titles and message text arrive from other sessions; none of it
# may ever reach the terminal as raw escape bytes.
msg_render_program() {
  cat <<'PY'
import json
import shutil
import sys
import time
import unicodedata

mode = sys.argv[1] if len(sys.argv) > 1 else ""


def fail(message):
    sys.stderr.write("session-kit: " + message + "\n")
    raise SystemExit(1)


try:
    payload = json.load(sys.stdin)
except (ValueError, OSError):
    fail("the messaging core returned output that is not JSON")

size = shutil.get_terminal_size(fallback=(100, 24))
width = size.columns
if width < 40:
    width = 40
height = size.lines
if height < 10:
    height = 10


def clean(text):
    text = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in str("" if text is None else text)
    )
    return " ".join(text.split())


def display_width(text):
    return sum(
        0 if unicodedata.combining(character)
        else 2 if unicodedata.east_asian_width(character) in {"W", "F"}
        else 1
        for character in text
    )


def clip(text, room):
    """Truncate to a cell budget, leaving already-laid-out spacing alone."""
    if room <= 0:
        return ""
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


def shorten(text, room):
    return clip(clean(text), room)


def pad(text, room):
    text = shorten(text, room)
    return text + " " * max(0, room - display_width(text))


def wrap_all(text, room):
    """Every line the text needs at this width, split on spaces where it can."""
    text = clean(text)
    if not text or room <= 0:
        return []
    lines = []
    current = ""
    for word in text.split(" "):
        while display_width(word) > room:
            kept = ""
            for character in word:
                if display_width(kept + character) > room:
                    break
                kept += character
            if not kept:
                kept = word[0]
            if current:
                lines.append(current)
                current = ""
            lines.append(kept)
            word = word[len(kept):]
        if not word:
            continue
        candidate = word if not current else current + " " + word
        if display_width(candidate) <= room:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def wrap_limited(text, room, limit):
    lines = wrap_all(text, room)
    if len(lines) <= limit:
        return lines
    kept = lines[:limit]
    kept[-1] = shorten(kept[-1] + " …", room)
    return kept


def age_text(milliseconds):
    seconds = int(milliseconds) // 1000
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    if seconds < 86400:
        return "%dh %dm" % (seconds // 3600, (seconds % 3600) // 60)
    return "%dd %dh" % (seconds // 86400, (seconds % 86400) // 3600)


def stamp(value, form="%Y-%m-%d %H:%M"):
    try:
        return time.strftime(form, time.localtime(int(value) / 1000))
    except (TypeError, ValueError, OSError, OverflowError):
        return "-"


def rows_of(document, key="targets"):
    value = document.get(key) if isinstance(document, dict) else None
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def selector_label(row, ordinal):
    # The first column is the key the operator can actually TYPE: the main
    # menu's terminal number, or the x-key for a session outside the manager.
    # A bare row index looks typeable and is not — it cost a drill a cycle.
    number = row.get("terminal_number")
    if isinstance(number, int) and not isinstance(number, bool) and number > 0:
        return str(number)
    return "x%d" % ordinal


def session_label(row):
    # The terminal number is how the operator knows a session everywhere else.
    # A session with no number gets no label rather than a raw shpool id: the
    # provider and title columns beside this one already say which one it is.
    number = row.get("terminal_number")
    if isinstance(number, int) and not isinstance(number, bool) and number > 0:
        return "#%d" % number
    return "-"


def provider_label(row):
    provider = clean(row.get("provider")) or "unknown"
    return provider


# A message the sender handed over whose arrival the target has not shown yet.
# It is neither delivered nor undelivered, and counting it as either would
# invite a repeat of something that already landed.
UNCONFIRMED = "landed-unconfirmed"


def unconfirmed_count(rows):
    return sum(1 for row in rows if row.get("status") == UNCONFIRMED)


def unconfirmed_note(rows):
    count = unconfirmed_count(rows)
    return "  ·  %d landed, unconfirmed" % count if count else ""


def status_label(row):
    for key in ("status", "agent_status"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return clean(value)
    return "-"


def render_table(header, rows, indent="  "):
    if not rows:
        return []
    columns = len(header)
    fixed = [display_width(clean(header[index])) for index in range(columns - 1)]
    for row in rows:
        for index in range(columns - 1):
            fixed[index] = max(fixed[index], display_width(clean(row[index])))
    room = width - len(indent) - sum(fixed) - 2 * (columns - 1)
    room = max(12, room)
    lines = []
    for row in [header] + rows:
        cells = []
        for index in range(columns - 1):
            cell = clean(row[index])
            cells.append(cell + " " * (fixed[index] - display_width(cell)))
        cells.append(shorten(row[columns - 1], room))
        lines.append((indent + "  ".join(cells)).rstrip())
    return lines


def counts_of(document, targets):
    counts = document.get("counts") if isinstance(document, dict) else None
    if not isinstance(counts, dict):
        counts = {}
    resolved = {}
    for key in ("claude", "codex", "unreachable_hint"):
        value = counts.get(key)
        resolved[key] = value if isinstance(value, int) and value >= 0 else None
    if resolved["claude"] is None:
        resolved["claude"] = sum(
            1 for row in targets if row.get("provider") == "claude"
        )
    if resolved["codex"] is None:
        resolved["codex"] = sum(
            1 for row in targets if row.get("provider") == "codex"
        )
    if resolved["unreachable_hint"] is None:
        resolved["unreachable_hint"] = 0
    return resolved


def thread_entry(document, row):
    """One target's thread, wherever the core hung it.

    The report can carry each thread beside its target row or in a `threads`
    map keyed by thread_key, and that map holds either the lines themselves or
    a {unread, lines} record. All three read the same here.
    """
    threads = document.get("threads")
    key = row.get("thread_key")
    if isinstance(threads, dict) and isinstance(key, str):
        entry = threads.get(key)
        if isinstance(entry, dict):
            return entry
        if isinstance(entry, list):
            return {"lines": entry}
    return {}


def thread_lines_for(document, row):
    lines = row.get("thread")
    if not isinstance(lines, list):
        lines = thread_entry(document, row).get("lines")
    if not isinstance(lines, list):
        return []
    return [line for line in lines if isinstance(line, dict)]


def unread_for(document, row):
    flag = row.get("unread")
    if isinstance(flag, bool):
        return flag
    flag = thread_entry(document, row).get("unread")
    if isinstance(flag, bool):
        return flag
    unread = document.get("unread")
    key = row.get("thread_key")
    if isinstance(unread, list) and isinstance(key, str):
        return key in unread
    if isinstance(unread, dict) and isinstance(key, str):
        return bool(unread.get(key))
    return False


def emit(line=""):
    print(line)


if mode == "count":
    targets = rows_of(payload)
    counts = counts_of(payload, targets)
    skipped = rows_of(payload, "skipped")
    emit(
        "%d %d %d %d %d"
        % (
            len(targets),
            counts["claude"],
            counts["codex"],
            counts["unreachable_hint"],
            len(skipped),
        )
    )
elif mode == "targets":
    targets = rows_of(payload)
    counts = counts_of(payload, targets)
    skipped = rows_of(payload, "skipped")
    headline = "Targets: %d  ·  claude %d  ·  codex %d" % (
        len(targets),
        counts["claude"],
        counts["codex"],
    )
    if counts["unreachable_hint"]:
        headline += "  ·  %d may be unreachable" % counts["unreachable_hint"]
    emit(headline)
    # Resolution rows do not always carry an agent status. A column of dashes
    # would cost width and say nothing, so it appears only when it has
    # something to report.
    with_status = any(status_label(row) != "-" for row in targets)
    header = ["#", "provider", "session"]
    if with_status:
        header.append("status")
    header.append("title")
    rows = []
    for index, row in enumerate(targets, 1):
        cells = [selector_label(row, index), provider_label(row), session_label(row)]
        if with_status:
            cells.append(status_label(row))
        cells.append(row.get("title"))
        rows.append(cells)
    for line in render_table(header, rows):
        emit(line)
    if skipped:
        emit("Skipped %d row(s) that are not live Claude or Codex sessions:" % len(skipped))
        for line in render_table(
            ["session", "provider", "reason"],
            [
                [
                    session_label(row),
                    provider_label(row),
                    row.get("reason") or row.get("detail"),
                ]
                for row in skipped
            ],
        ):
            emit(line)
elif mode == "receipts":
    targets = rows_of(payload)
    message_id = clean(payload.get("msg_id")) if isinstance(payload, dict) else ""
    delivered = sum(
        1 for row in targets if str(row.get("status") or "").startswith("delivered")
    )
    unreachable = sum(
        1
        for row in targets
        if row.get("status") in ("unreachable", "failed", "ambiguous")
    )
    emit(
        "Sent %s to %d target(s)  ·  delivered %d  ·  not delivered %d%s"
        % (
            message_id or "(no id)",
            len(targets),
            delivered,
            unreachable,
            unconfirmed_note(targets),
        )
    )
    for line in render_table(
        ["#", "provider", "session", "status", "detail"],
        [
            [
                selector_label(row, index),
                provider_label(row),
                session_label(row),
                status_label(row),
                row.get("detail"),
            ]
            for index, row in enumerate(targets, 1)
        ],
    ):
        emit(line)
    # A `keys:` send skips a target whose session has exited rather than
    # failing the whole follow-up, so the receipt is the only place that
    # departure is ever reported.
    departed = rows_of(payload, "skipped")
    if departed:
        emit("Not sent to %d session(s):" % len(departed))
        for line in render_table(
            ["session", "provider", "reason"],
            [
                [
                    session_label(row),
                    provider_label(row),
                    row.get("reason") or row.get("detail"),
                ]
                for row in departed
            ],
        ):
            emit(line)
    if message_id:
        emit("Replies arrive in: sp msg report %s" % message_id)
elif mode == "report":
    if not isinstance(payload, dict) or not payload.get("msg_id"):
        emit("No message has been sent yet.")
        raise SystemExit(0)
    # The send record may be the document itself or nested under "send".
    record = payload.get("send")
    if not isinstance(record, dict):
        record = payload
    targets = rows_of(record)
    message_id = clean(payload.get("msg_id") or record.get("msg_id"))
    replied = sum(1 for row in targets if row.get("status") == "replied")
    unreachable = sum(
        1
        for row in targets
        if row.get("status") in ("unreachable", "failed", "ambiguous")
    )
    emit(
        "Message %s  ·  sent %s  ·  %d target(s)  ·  %d replied  ·  %d not delivered%s"
        % (
            message_id,
            stamp(record.get("created_unix_ms")),
            len(targets),
            replied,
            unreachable,
            unconfirmed_note(targets),
        )
    )
    emit("Text: " + shorten(record.get("operator_text"), width - 6))
    if record.get("fyi"):
        emit("Sent as --fyi: no reply was requested.")
    emit()
    for index, row in enumerate(targets, 1):
        marker = "  ✉ new reply" if unread_for(payload, row) else ""
        emit(
            shorten(
                "  %d  %s  %s  %s  %s%s"
                % (
                    index,
                    provider_label(row),
                    session_label(row),
                    status_label(row),
                    clean(row.get("title")),
                    marker,
                ),
                width,
            )
        )
        detail = clean(row.get("detail"))
        if detail:
            emit(shorten("       " + detail, width))
        for line in thread_lines_for(payload, row):
            direction = "out" if line.get("dir") == "out" else "in "
            emit(
                shorten(
                    "       %s %s  %s"
                    % (
                        direction,
                        stamp(line.get("ts_unix_ms"), "%H:%M"),
                        clean(line.get("text")),
                    ),
                    width,
                )
            )
        emit()
    emit("Reply from an agent session with: sp msg reply %s \"one line\"" % message_id)
elif mode == "list":
    sends = payload if isinstance(payload, list) else rows_of(payload, "sends")
    if not sends:
        emit("No messages have been sent yet.")
        raise SystemExit(0)
    for line in render_table(
        ["id", "sent", "targets", "replied", "undelivered", "text"],
        [
            [
                clean(row.get("msg_id")),
                stamp(row.get("created_unix_ms")),
                str(row.get("n_targets") if isinstance(row.get("n_targets"), int) else "-"),
                str(row.get("n_replied") if isinstance(row.get("n_replied"), int) else "-"),
                str(
                    row.get("n_unreachable")
                    if isinstance(row.get("n_unreachable"), int)
                    else "-"
                ),
                row.get("preview"),
            ]
            for row in sends
        ],
    ):
        emit(line)
elif mode == "id":
    emit(clean(payload.get("msg_id")) if isinstance(payload, dict) else "")
elif mode == "pick":
    # The recent sends, numbered so one keypress opens one of them. Same
    # protocol as the console: the map first, prefixed by its length, so no
    # message text can be mistaken for a separator.
    sends = payload if isinstance(payload, list) else rows_of(payload, "sends")
    index = [clean(row.get("msg_id")) for row in sends]
    emit(str(len(index)))
    for identifier in index:
        emit(identifier)
    if not sends:
        emit("No messages have been sent yet.")
    else:
        emit("Recent messages")
        for line in render_table(
            ["#", "id", "sent", "targets", "replied", "text"],
            [
                [
                    str(position),
                    clean(row.get("msg_id")),
                    stamp(row.get("created_unix_ms")),
                    str(row.get("n_targets") if isinstance(row.get("n_targets"), int) else "-"),
                    str(row.get("n_replied") if isinstance(row.get("n_replied"), int) else "-"),
                    row.get("preview"),
                ]
                for position, row in enumerate(sends, 1)
            ],
        ):
            emit(line)
elif mode == "unread-keys":
    # The thread keys this report shows as unread. `msg report` no longer
    # clears anything, so the static view marks exactly what it printed.
    document = payload if isinstance(payload, dict) else {}
    record = document.get("send")
    if not isinstance(record, dict):
        record = document
    for row in rows_of(record):
        key = row.get("thread_key")
        if isinstance(key, str) and key and unread_for(document, row):
            emit(clean(key))
elif mode == "console":
    # One frame of the live console, plus the map the shell needs to turn a
    # keypress into a thread. The map comes FIRST, prefixed by its own length,
    # so no title or reply text can ever be mistaken for a separator:
    #
    #   <rows> <msg_id>
    #   <ordinal>\t<thread_key>   × rows
    #   <screen…>
    #
    # The ordinal is the target's position in the stored send record, never its
    # position on screen. Rows re-sort as replies land, and a number that moved
    # under the operator's finger between two redraws would open the wrong
    # thread.
    seconds = clean(sys.argv[2]) if len(sys.argv) > 2 else "2"
    notice = clean(sys.argv[3]) if len(sys.argv) > 3 else ""
    document = payload if isinstance(payload, dict) else {}
    record = document.get("send")
    if not isinstance(record, dict):
        record = document
    targets = rows_of(record)
    message_id = clean(record.get("msg_id") or document.get("msg_id"))
    index = []
    screen = []
    if not message_id or not targets:
        if notice:
            screen.append(shorten(notice, width))
        screen.append("No message has been sent yet.")
        screen.append("")
        screen.append("n  write one   ·   l  recent messages   ·   Enter  back")
    else:
        created = record.get("created_unix_ms")
        try:
            age = age_text(int(time.time() * 1000) - int(created))
        except (TypeError, ValueError):
            age = ""
        delivered = sum(
            1 for row in targets if str(row.get("status") or "").startswith("delivered")
        )
        replied = sum(1 for row in targets if row.get("status") == "replied")
        undelivered = sum(
            1
            for row in targets
            if row.get("status") in ("unreachable", "failed", "ambiguous")
        )
        rows = []
        for ordinal, row in enumerate(targets, 1):
            key = row.get("thread_key")
            key = clean(key) if isinstance(key, str) else ""
            lines = thread_lines_for(document, row)
            inbound = [line for line in lines if line.get("dir") != "out"]
            number = row.get("terminal_number")
            # The selector IS the terminal number from the main menu — one
            # session, one number, everywhere. A target with no terminal
            # number (outside the manager) gets an x-key so it can never
            # collide with a menu number.
            selector = (
                str(number)
                if isinstance(number, int)
                and not isinstance(number, bool)
                and number > 0
                else "x%d" % ordinal
            )
            rows.append(
                {
                    "ordinal": ordinal,
                    "selector": selector,
                    "key": key,
                    # Straight from the ledger. Polling this report changes
                    # nothing there, so the envelope stays until the thread is
                    # opened and the console never disagrees with the count
                    # the picker shows in its header.
                    "unread": bool(unread_for(document, row)),
                    "answered": row.get("status") == "replied" or bool(inbound),
                    "provider": provider_label(row),
                    "status": status_label(row),
                    "title": clean(row.get("title")),
                    "latest": (
                        clean(inbound[-1].get("text"))
                        if inbound
                        else clean(row.get("detail"))
                    ),
                    "order": (
                        number
                        if isinstance(number, int)
                        and not isinstance(number, bool)
                        and number > 0
                        else 1_000_000
                    ),
                }
            )
        for item in rows:
            index.append("%s\t%s" % (item["selector"], item["key"]))
        # Still owing an answer first, then by terminal number.
        ordered = sorted(
            rows, key=lambda item: (item["answered"], item["order"], item["ordinal"])
        )
        screen.append(
            shorten(
                "Message %s · sent %s%s"
                % (message_id, stamp(created), " (%s ago)" % age if age else ""),
                width,
            )
        )
        screen.append(
            shorten(
                "%d target(s) · %d delivered · %d replied · %d not delivered%s"
                % (
                    len(targets),
                    delivered,
                    replied,
                    undelivered,
                    unconfirmed_note(targets).replace("  ·  ", " · "),
                ),
                width,
            )
        )
        for position, line in enumerate(
            wrap_limited(record.get("operator_text"), max(8, width - 6), 2)
        ):
            screen.append(("Text: " if position == 0 else "      ") + line)
        if record.get("fyi"):
            screen.append("Sent as --fyi: no reply was requested.")
        screen.append("")
        number_room = max(1, max(len(item["selector"]) for item in ordered))

        def wanted(field, header_width, cap):
            return max(
                1,
                min(cap, max([display_width(item[field]) for item in rows] + [header_width])),
            )

        # Every row must fit the window: one wrapped line would push the frame
        # past the height the rows were just budgeted against. Columns are
        # given what they want, then shrunk in this order until they fit.
        room = {
            "provider": wanted("provider", 8, 8),
            "status": wanted("status", 6, 16),
            "title": wanted("title", 5, 40),
            "latest": wanted("latest", 12, 60),
        }
        floor = {"provider": 4, "status": 6, "title": 8, "latest": 0}
        # 2 indent + marker + space + number + 2, then three 2-cell gaps.
        budget = max(24, width - 12 - number_room)
        for field in ("latest", "status", "title", "provider"):
            over = sum(room.values()) - budget
            if over <= 0:
                break
            room[field] = max(floor[field], room[field] - over)

        def console_row(marker, number, item):
            cells = [
                "  %s %s  " % (marker, str(number).rjust(number_room)),
                pad(item["provider"], room["provider"]),
                "  ",
                pad(item["status"], room["status"]),
                "  ",
                pad(item["title"], room["title"]),
            ]
            if room["latest"]:
                cells.append("  " + shorten(item["latest"], room["latest"]))
            # The column budget already fits, but a row is never allowed to
            # wrap: one wrapped row and the frame is a line taller than the
            # height the rows were counted against. Clipped, not shortened —
            # the cells are laid out already and their padding must survive.
            return clip("".join(cells).rstrip(), width)

        header = {
            "provider": "provider",
            "status": "status",
            "title": "title",
            "latest": "newest reply",
        }
        screen.append(console_row(" ", "#", header))
        # Two footer lines, a blank, and whatever notice is pending: rows are
        # capped so the frame never scrolls, because a scrolled frame leaves
        # the previous redraw's tail on screen.
        room_for_rows = max(1, height - len(screen) - 4 - (1 if notice else 0))
        hidden = 0
        if len(ordered) > room_for_rows:
            hidden = len(ordered) - (room_for_rows - 1)
            ordered = ordered[: room_for_rows - 1]
        for item in ordered:
            screen.append(
                console_row("✉" if item["unread"] else " ", item["ordinal"], item)
            )
        if hidden:
            screen.append(
                "  (%d more target(s) — a taller window shows them)" % hidden
            )
        if notice:
            screen.append(shorten(notice, width))
        screen.append("")
        screen.append(
            shorten(
                "number  thread   ·   n  new   ·   a  follow up to all"
                "   ·   l  recent   ·   r  refresh   ·   Enter  back"
                if width >= 80
                else "number thread · n new · a all · l recent · Enter back",
                width,
            )
        )
        legend = (
            "✉ is a reply you have not opened. "
            if any(item["unread"] for item in rows)
            else ""
        )
        screen.append(
            shorten(
                "%sUpdating every %ss. Enter goes back; q works too." % (legend, seconds),
                width,
            )
        )
    emit("%d %s" % (len(index), message_id))
    for line in index:
        emit(line)
    for line in screen:
        emit(line)
elif mode == "thread":
    wanted = sys.argv[2] if len(sys.argv) > 2 else ""
    label = clean(sys.argv[3]) if len(sys.argv) > 3 else "?"
    document = payload if isinstance(payload, dict) else {}
    record = document.get("send")
    if not isinstance(record, dict):
        record = document
    row = None
    for candidate in rows_of(record):
        if candidate.get("thread_key") == wanted:
            row = candidate
            break
    if row is None:
        emit("That target is no longer part of this message.")
        emit("")
        emit("Press Enter to go back.")
    else:
        emit(
            shorten(
                "Thread %s · %s · %s"
                % (
                    "#" + label if label.isdigit() else label,
                    provider_label(row),
                    status_label(row),
                ),
                width,
            )
        )
        emit(shorten("  " + clean(row.get("title")), width))
        detail = clean(row.get("detail"))
        if detail:
            emit(shorten("  " + detail, width))
        emit("")
        lines = thread_lines_for(document, row)
        if not lines:
            emit("  Nothing has been recorded on this thread yet.")
        for line in lines:
            head = "  %s %s  " % (
                "out" if line.get("dir") == "out" else "in ",
                stamp(line.get("ts_unix_ms"), "%H:%M"),
            )
            text = line.get("text")
            # Threads written before 2026-08-08 logged the whole delivery
            # envelope as the outgoing line. Show only the operator's words.
            if (
                line.get("dir") == "out"
                and isinstance(text, str)
                and text.startswith("[session-kit operator message")
                and "\n---\n" in text
            ):
                text = text.split("\n---\n", 1)[1]
            body = wrap_all(text, max(8, width - display_width(head)))
            emit(head + (body[0] if body else ""))
            for extra in body[1:]:
                emit(" " * display_width(head) + extra)
        emit("")
        emit("Type a reply and press Enter. An empty line goes back.")
else:
    fail("unknown render mode")
PY
}

msg_render() {
  local mode=$1 program
  shift
  program=$(msg_render_program) || return 1
  python3 -c "$program" "$mode" "$@"
}

msg_render_targets() { msg_render targets; }
msg_render_receipts() { msg_render receipts; }
msg_render_report() { msg_render report; }
msg_render_list() { msg_render list; }

# One y/N for a whole broadcast. The single-character read leaves the human's
# Enter in the terminal buffer, where the caller's next read — the picker menu,
# for one — would consume it as an empty choice. Drain the rest of the line;
# that leftover newline silently ended the picker after every kill once already.
msg_confirm_mass() {
  local prompt=$1 answer=""
  printf '\n  %s\n' "$prompt"
  read -r -n 1 -p "  Confirm? [y/N] " answer
  if [[ $answer != "" && $answer != $'\n' ]]; then
    local rest=""
    IFS= read -r -s -t 0.3 rest 2>/dev/null || true
  fi
  printf '\n'
  [[ $answer == y || $answer == Y ]]
}

msg_send_command() {
  local fyi=0 assume_yes=0
  local -a positional=()
  while (( $# > 0 )); do
    case "$1" in
      --fyi)
        fyi=1
        ;;
      --yes | -y)
        assume_yes=1
        ;;
      --)
        shift
        while (( $# > 0 )); do
          positional+=("$1")
          shift
        done
        break
        ;;
      -*)
        msg_usage >&2
        return 2
        ;;
      *)
        positional+=("$1")
        ;;
    esac
    shift
  done
  if (( ${#positional[@]} != 2 )); then
    msg_usage >&2
    return 2
  fi
  local target=${positional[0]} text=${positional[1]}
  if [[ -z $target || -z $text ]]; then
    msg_usage >&2
    return 2
  fi
  # The core refuses too; refusing here as well keeps the switch from being
  # discovered only after a broadcast has been resolved and confirmed.
  if [[ ${SESSION_KIT_MSG:-1} == 0 ]]; then
    sk_die "messaging is switched off (SESSION_KIT_MSG=0); nothing was resolved or sent"
    return 1
  fi

  local resolved
  resolved=$(python3 "$INVENTORY_CORE" msg resolve --target "$target") || {
    sk_die "could not resolve message targets; nothing was sent"
    return 1
  }
  local summary
  summary=$(printf '%s' "$resolved" | msg_render count) || {
    sk_die "could not read the resolved target list; nothing was sent"
    return 1
  }
  local total claude codex unreachable_hint skipped
  read -r total claude codex unreachable_hint skipped <<<"$summary"
  printf '%s' "$resolved" | msg_render_targets || return 1

  # `all` and `idle` are open-ended and need the confirmation. A typed number
  # list is not: the operator named every session it reaches, and the resolved
  # target table is printed above this line before anything is sent.
  local mass=0
  if [[ $target == all || $target == idle || $target == a ]]; then
    mass=1
  fi
  if (( total == 0 )); then
    if (( mass == 1 )); then
      printf 'No live Claude or Codex session matched "%s"; nothing was sent.\n' "$target"
      return 0
    fi
    sk_die "no live Claude or Codex session matched \"$target\"; nothing was sent"
    return 1
  fi
  if (( mass == 1 && assume_yes == 0 )); then
    if [[ ${SESSION_KIT_NONINTERACTIVE:-0} == 1 || ! -t 0 ]]; then
      sk_die "a mass send needs a terminal to confirm on; re-run with --yes to send $total message(s) without one"
      return 1
    fi
    local prompt
    prompt=$(printf 'Send this message to %s session(s): claude %s, codex %s' \
      "$total" "$claude" "$codex")
    msg_confirm_mass "$prompt" || {
      echo "Send cancelled; nothing was delivered."
      return 1
    }
  fi

  local -a send_args=(msg send --target "$target" --text "$text")
  if (( fyi == 1 )); then
    send_args+=(--fyi)
  fi
  # A caller that must repeat a send until it can prove delivery sets this for
  # the one command, exactly as `sp close` is told its confirmation id. The
  # core then resumes that purpose's message rather than writing a second one,
  # and leaves alone any target the ledger already shows as landed. No
  # operator flag: a person typing a message wants a new message.
  if [[ -n ${SESSION_KIT_MSG_KEY:-} ]]; then
    send_args+=(--key "$SESSION_KIT_MSG_KEY")
  fi
  local receipts
  receipts=$(python3 "$INVENTORY_CORE" "${send_args[@]}") || {
    sk_die "the send did not complete; check 'sp msg list' before repeating it so nothing is delivered twice"
    return 1
  }
  # The message centre reads this to switch its view to what was just sent,
  # so a person who writes a message is already watching for its answers.
  MSG_LAST_SEND_ID=$(printf '%s' "$receipts" | msg_render id) || MSG_LAST_SEND_ID=""
  printf '%s' "$receipts" | msg_render_receipts
}

# Split from the render so the Phase 2 console can poll this and re-render
# without duplicating either half.
msg_fetch_report() {
  local id=${1:-}
  if [[ -n $id ]]; then
    python3 "$INVENTORY_CORE" msg report --id "$id"
  else
    python3 "$INVENTORY_CORE" msg report
  fi
}

# ---- live console ------------------------------------------------------
# `sp msg report` on a terminal is a console: the same fetch and the same
# renderer as the static view, wrapped in a poll loop. Every action it offers
# goes back out through the core's own verbs, so the console never becomes a
# second implementation of delivery, of the ledger, or of the layout.
#
# State the loop and its helpers share. Unread is NOT among it: reading a
# report leaves the ledger alone, so the console shows the marks the ledger
# holds and clears one only by asking for it — the same marks the picker
# counts in its header, which is why the console must not keep a private idea
# of them.
MSG_CONSOLE_INTERRUPTED=0
MSG_CONSOLE_READ=""
MSG_CONSOLE_INDEX=""
MSG_CONSOLE_NOTICE=""
MSG_CONSOLE_MSG_ID=""
MSG_CONSOLE_NUMBER=""
# Which message the centre is watching. Writing a new one or picking an older
# one moves it, and the next poll draws that message instead.
MSG_CONSOLE_ID=""
MSG_CONSOLE_OFFERED_NEW=0
MSG_LAST_SEND_ID=""

msg_console_enabled() {
  [[ ${SESSION_KIT_MSG_CONSOLE:-1} != 0 ]] || return 1
  [[ ${SESSION_KIT_NONINTERACTIVE:-0} != 1 ]] || return 1
  [[ -t 0 && -t 1 ]] || return 1
  # A redraw REPLACES the frame. A terminal that cannot be cleared would stack
  # copies down the screen instead — the same reason the picker refuses to
  # poll without a capable terminal.
  [[ -n ${TERM:-} && ${TERM,,} != dumb ]] || return 1
}

msg_console_seconds() {
  local seconds=${SESSION_KIT_MSG_CONSOLE_SECONDS:-2}
  [[ $seconds =~ ^[0-9]+(\.[0-9]+)?$ && $seconds != 0 ]] || seconds=2
  printf '%s' "$seconds"
}

msg_console_clear() {
  printf '\033[2J\033[H'
}

# Picker precedent: no exit from an interactive surface is allowed to be
# silent. The action log is the only place a "it just dropped me back to the
# shell" report can be diagnosed from afterwards.
msg_console_note() {
  sk_log_action msg_console "$1" || true
}

msg_console_key_for() {
  local wanted=$1 ordinal key
  while IFS=$'\t' read -r ordinal key; do
    [[ $ordinal == "$wanted" && -n $key ]] || continue
    printf '%s' "$key"
    return 0
  done <<<"$MSG_CONSOLE_INDEX"
  return 1
}

msg_console_keys() {
  local ordinal key
  while IFS=$'\t' read -r ordinal key; do
    [[ -n $key ]] || continue
    printf '%s\n' "$key"
  done <<<"$MSG_CONSOLE_INDEX"
}

# Clearing an unread mark is a deliberate act, never a side effect of looking.
# Failing to clear one is not worth interrupting anybody over: the mark simply
# stays, which is the truthful answer either way.
msg_mark_thread_read() {
  local key=$1
  [[ -n $key ]] || return 0
  python3 "$INVENTORY_CORE" msg mark-read --thread "$key" >/dev/null 2>&1 || return 0
}

# One frame: render, adopt the number-to-thread map it came with, then replace
# the screen. Nothing is printed unless the whole frame rendered.
msg_console_draw() {
  local payload=$1 seconds=$2 notice=${3:-}
  local -a rendered=()
  mapfile -t rendered < <(
    printf '%s' "$payload" | msg_render console "$seconds" "$notice"
  )
  (( ${#rendered[@]} > 0 )) || return 1
  local count message_id
  read -r count message_id <<<"${rendered[0]}"
  [[ $count =~ ^[0-9]+$ ]] || return 1
  (( ${#rendered[@]} > count )) || return 1
  MSG_CONSOLE_MSG_ID=$message_id
  MSG_CONSOLE_INDEX=""
  local position
  for (( position = 1; position <= count; position++ )); do
    MSG_CONSOLE_INDEX+="${rendered[position]}"$'\n'
  done
  msg_console_clear
  if (( ${#rendered[@]} > count + 1 )); then
    printf '%s\n' "${rendered[@]:count+1}"
  fi
}

# Single key, with the timeout doubling as the poll clock. A bare Enter reads
# as an empty key and simply redraws, which also absorbs the newline left over
# from a confirm — the leftover that silently ended the picker once already.
msg_console_read_key() {
  local __sk_key_var=$1 seconds=$2 status=0
  MSG_CONSOLE_READ=key
  # An interrupt that landed while the last frame was being drawn is still an
  # interrupt. Clearing the flag on the way in — instead of answering it —
  # loses that Ctrl-C silently, and which one is lost depends on how busy the
  # machine is.
  if (( MSG_CONSOLE_INTERRUPTED )); then
    MSG_CONSOLE_INTERRUPTED=0
    MSG_CONSOLE_READ=interrupt
    return 1
  fi
  # The status MUST be captured in the else. After a bare `if` whose condition
  # failed, $? is the status of the `if` itself — zero — and a timed-out poll
  # would read as closed input and quietly end the console every 2 seconds.
  # The picker learned this the same way.
  # The caller passes the DESTINATION variable name.
  # shellcheck disable=SC2229
  if IFS= read -r -s -n 1 -t "$seconds" "$__sk_key_var"; then
    return 0
  else
    status=$?
  fi
  if (( MSG_CONSOLE_INTERRUPTED )); then
    MSG_CONSOLE_INTERRUPTED=0
    MSG_CONSOLE_READ=interrupt
    return 1
  fi
  if (( status > 128 )); then
    MSG_CONSOLE_READ=timeout
    return 1
  fi
  MSG_CONSOLE_READ=closed
  return 1
}

# Target lists run past nine, so a digit collects the digits typed with it. A
# short gap, a non-digit, or Enter ends the number. The answer goes in a
# global rather than through a command substitution: that subshell would take
# the default SIGINT action and die on the Ctrl-C this console absorbs.
msg_console_read_number() {
  local extra=""
  MSG_CONSOLE_NUMBER=$1
  while (( ${#MSG_CONSOLE_NUMBER} < 4 )); do
    IFS= read -r -s -n 1 -t 0.6 extra || break
    [[ $extra == [0-9] ]] || break
    MSG_CONSOLE_NUMBER+=$extra
  done
}

msg_console_read_line() {
  local __sk_line_var=$1 prompt=$2
  MSG_CONSOLE_READ=line
  if (( MSG_CONSOLE_INTERRUPTED )); then
    MSG_CONSOLE_INTERRUPTED=0
    MSG_CONSOLE_READ=interrupt
    return 1
  fi
  # shellcheck disable=SC2229
  if IFS= read -r -e -p "$prompt" "$__sk_line_var"; then
    return 0
  fi
  printf '\n'
  if (( MSG_CONSOLE_INTERRUPTED )); then
    MSG_CONSOLE_INTERRUPTED=0
    MSG_CONSOLE_READ=interrupt
    return 1
  fi
  MSG_CONSOLE_READ=closed
  return 1
}

msg_console_drain() {
  local rest=""
  IFS= read -r -s -t 0.3 rest 2>/dev/null || true
}

msg_console_pause() {
  local seconds=$1 rest=""
  printf '\n  Press a key to go back…'
  IFS= read -r -s -n 1 -t "$seconds" rest 2>/dev/null || true
  printf '\n'
}

# One target, one send: the same core verb the command line uses, with the
# thread key as the selector so nothing else can be reached from here.
msg_console_send() {
  local key=$1 text=$2 receipts
  if [[ ${SESSION_KIT_MSG:-1} == 0 ]]; then
    printf '  Messaging is switched off (SESSION_KIT_MSG=0); nothing was sent.\n'
    return 1
  fi
  printf '\n  Sending… a Claude target runs a headless sender, which can take a minute.\n'
  receipts=$(python3 "$INVENTORY_CORE" msg send --target "key:$key" --text "$text") || {
    msg_console_note send_failed
    printf '  The send did not complete. Check sp msg list before repeating it.\n'
    return 1
  }
  printf '%s' "$receipts" | msg_render_receipts || true
  msg_console_note send_recorded
}

# Opening a thread is what clears its unread mark, and the only thing that
# does. The mark lives in the ledger, where the picker counts it for the
# header cue, so the console never holds an idea of its own about which
# replies are new -- it shows what the ledger says and clears one only here.
# A later reply sets it again.
msg_console_open_thread() {
  local id=$1 ordinal=$2 seconds=$3 key payload reply
  key=$(msg_console_key_for "$ordinal") || key=""
  if [[ -z $key ]]; then
    MSG_CONSOLE_NOTICE="No target is numbered $ordinal in this message."
    return 0
  fi
  msg_mark_thread_read "$key"
  while true; do
    payload=$(msg_fetch_report "$id") || {
      msg_console_note report_failed
      sk_die "could not read that message report"
      return 1
    }
    msg_console_clear
    printf '%s' "$payload" | msg_render thread "$key" "$ordinal" || {
      msg_console_note render_failed
      sk_die "the message console could not render that thread"
      return 1
    }
    reply=""
    if ! msg_console_read_line reply "  reply> "; then
      # An interrupt is "go back". Input that has actually closed is the end
      # of the console, said out loud, exactly as the picker ends.
      [[ $MSG_CONSOLE_READ != closed ]] || return 2
      return 0
    fi
    [[ -n ${reply//[[:space:]]/} ]] || return 0
    msg_console_send "$key" "$reply" || true
    msg_console_pause "$seconds"
  done
}

# A follow-up is ONE linked send: the facade's keys: selector carries every
# target of this message in a single call, so a batch of Claude targets costs
# one headless run, not one per target. A target whose session has exited is
# skipped and reported by the core, never fatal to the rest.
msg_console_followup() {
  local seconds=$1 text="" key joined=""
  local -a keys=()
  mapfile -t keys < <(msg_console_keys)
  if (( ${#keys[@]} == 0 )); then
    MSG_CONSOLE_NOTICE="This message has no targets to follow up."
    return 0
  fi
  msg_console_drain
  printf '\n  Follow up with all %d target(s) of message %s.\n' \
    "${#keys[@]}" "${MSG_CONSOLE_MSG_ID:-(unknown)}"
  if ! msg_console_read_line text "  message> "; then
    [[ $MSG_CONSOLE_READ != closed ]] || return 2
    return 0
  fi
  if [[ -z ${text//[[:space:]]/} ]]; then
    MSG_CONSOLE_NOTICE="Follow-up cancelled; nothing was sent."
    return 0
  fi
  local prompt
  prompt=$(printf 'Send this follow-up to %d target(s) of message %s' \
    "${#keys[@]}" "${MSG_CONSOLE_MSG_ID:-(unknown)}")
  msg_confirm_mass "$prompt" || {
    MSG_CONSOLE_NOTICE="Follow-up cancelled; nothing was sent."
    return 0
  }
  for key in "${keys[@]}"; do
    if [[ -z $joined ]]; then joined=$key; else joined+=",$key"; fi
  done
  if [[ ${SESSION_KIT_MSG:-1} == 0 ]]; then
    MSG_CONSOLE_NOTICE="Messaging is switched off (SESSION_KIT_MSG=0); nothing was sent."
    return 0
  fi
  printf '\n  Sending… Claude targets share one headless sender run.\n'
  local receipts
  if receipts=$(python3 "$INVENTORY_CORE" msg send \
    --target "keys:$joined" --text "$text"); then
    printf '%s' "$receipts" | msg_render_receipts || true
    msg_console_note send_recorded
    MSG_CONSOLE_NOTICE=$(printf 'Follow-up sent to the %d target(s) still live.' \
      "${#keys[@]}")
  else
    msg_console_note send_failed
    MSG_CONSOLE_NOTICE="The follow-up did not complete; sp msg list has the record."
  fi
  msg_console_pause "$seconds"
}

# Write a message from inside the centre and stay with it. The send itself is
# msg_send_command -- the same resolution, the same target table, the same one
# confirmation a broadcast needs, the same receipts -- so what a person sees
# here and what the command line does can never drift apart. When it lands,
# the view moves to the new message, because the reason for writing one is to
# read the answers.
msg_console_new() {
  local seconds=$1 answer="" target="" text=""
  msg_console_drain
  printf '\n  New message\n'
  printf '    a       every live Claude and Codex session\n'
  printf '    i       only the ones that are idle\n'
  printf '    number  one session\n'
  printf '    Enter   back\n\n'
  if ! msg_console_read_line answer "  target> "; then
    [[ $MSG_CONSOLE_READ != closed ]] || return 2
    return 0
  fi
  case "$answer" in
    '') return 0 ;;
    a | A) target=all ;;
    i | I) target=idle ;;
    *)
      if [[ $answer =~ ^[0-9]+$ ]]; then
        target=$answer
      else
        MSG_CONSOLE_NOTICE="Use a, i, or a session number. Nothing was sent."
        return 0
      fi
      ;;
  esac
  if ! msg_console_read_line text "  message> "; then
    [[ $MSG_CONSOLE_READ != closed ]] || return 2
    return 0
  fi
  if [[ -z ${text//[[:space:]]/} ]]; then
    MSG_CONSOLE_NOTICE="Nothing was sent."
    return 0
  fi
  MSG_LAST_SEND_ID=""
  msg_send_command "$target" "$text" || true
  if [[ -n $MSG_LAST_SEND_ID ]]; then
    MSG_CONSOLE_ID=$MSG_LAST_SEND_ID
    msg_console_note send_recorded
    MSG_CONSOLE_NOTICE="Watching $MSG_LAST_SEND_ID; replies appear here as they arrive."
  fi
  msg_console_pause "$seconds"
}

# The recent messages, one keypress to go back to any of them.
msg_console_pick() {
  local seconds=$1 answer="" chosen=""
  local -a rendered=() ids=()
  msg_console_drain
  mapfile -t rendered < <(python3 "$INVENTORY_CORE" msg list | msg_render pick)
  if (( ${#rendered[@]} == 0 )) || [[ ! ${rendered[0]} =~ ^[0-9]+$ ]]; then
    MSG_CONSOLE_NOTICE="The message list could not be read."
    return 0
  fi
  local count=${rendered[0]} position
  (( ${#rendered[@]} > count )) || return 0
  for (( position = 1; position <= count; position++ )); do
    ids+=("${rendered[position]}")
  done
  msg_console_clear
  printf '%s\n' "${rendered[@]:count+1}"
  printf '\n  A number opens that message. Enter goes back.\n\n'
  if ! msg_console_read_line answer "  message> "; then
    [[ $MSG_CONSOLE_READ != closed ]] || return 2
    return 0
  fi
  [[ -n $answer ]] || return 0
  if [[ $answer =~ ^[0-9]+$ ]] && (( answer >= 1 && answer <= count )); then
    chosen=${ids[answer-1]}
  fi
  if [[ -z $chosen ]]; then
    MSG_CONSOLE_NOTICE="No message is numbered $answer in that list."
    return 0
  fi
  MSG_CONSOLE_ID=$chosen
  return 0
}

msg_console_loop() {
  local seconds=$1
  local payload="" fresh="" failures=0 key="" inner=0 watching=""
  while true; do
    # The view can move underneath this loop: writing a message switches to
    # it, picking an older one goes back to that. A move throws away the
    # frame that belonged to the previous message.
    if [[ $MSG_CONSOLE_ID != "$watching" ]]; then
      watching=$MSG_CONSOLE_ID
      payload=""
      failures=0
    fi
    if fresh=$(msg_fetch_report "$MSG_CONSOLE_ID"); then
      payload=$fresh
      failures=0
    elif (( MSG_CONSOLE_INTERRUPTED )); then
      # Ctrl-C reaches the whole foreground group, so an interrupt that lands
      # while the core is being read kills the read too. That is an interrupt,
      # never evidence of a broken ledger, and it must not count against the
      # failure budget below.
      :
    else
      failures=$(( failures + 1 ))
      if [[ -z $payload ]]; then
        msg_console_note report_failed
        sk_die "could not read that message report"
        return 1
      fi
      # A refresh that fails once is not a reason to throw away a console the
      # operator is reading; five in a row is.
      if (( failures >= 5 )); then
        msg_console_note report_failed
        sk_die "the message report stopped answering; the console was left"
        return 1
      fi
      MSG_CONSOLE_NOTICE="The last refresh failed; this is the state that loaded before it."
    fi
    # An interrupt anywhere in the cycle says the same thing on the next frame.
    if (( MSG_CONSOLE_INTERRUPTED )); then
      MSG_CONSOLE_INTERRUPTED=0
      MSG_CONSOLE_NOTICE="Ctrl-C redraws this console. Press q to leave it."
    fi
    # Interrupted before the first report ever loaded: nothing to draw yet.
    [[ -n $payload ]] || continue
    if ! msg_console_draw "$payload" "$seconds" "$MSG_CONSOLE_NOTICE"; then
      # Ctrl-C kills the renderer along with everything else in the foreground
      # group. A half-drawn frame from an interrupt is a frame to draw again,
      # not a reason to end the console.
      if (( MSG_CONSOLE_INTERRUPTED )); then
        continue
      fi
      msg_console_note render_failed
      sk_die "the message console could not render that report"
      return 1
    fi
    # The bare console opens on the latest message, but the view must not
    # follow the ledger afterwards: with agents sending concurrently, a
    # refresh that jumps to someone else's newer message lands the operator's
    # keypress in the wrong thread. Pin to the message that first drew; n and
    # l are the deliberate ways forward.
    if [[ -z $MSG_CONSOLE_ID && -n $MSG_CONSOLE_MSG_ID ]]; then
      MSG_CONSOLE_ID=$MSG_CONSOLE_MSG_ID
      watching=$MSG_CONSOLE_ID
    fi
    MSG_CONSOLE_NOTICE=""
    # First run, nothing ever sent: the only useful thing here is writing a
    # message, so ask for one instead of showing an empty screen and waiting.
    if [[ -z $MSG_CONSOLE_MSG_ID ]] && (( MSG_CONSOLE_OFFERED_NEW == 0 )); then
      MSG_CONSOLE_OFFERED_NEW=1
      inner=0
      msg_console_new "$seconds" || inner=$?
      if (( inner == 2 )); then
        msg_console_note input_closed
        return 0
      fi
      (( inner == 0 )) || return "$inner"
      continue
    fi
    key=""
    if msg_console_read_key key "$seconds"; then
      inner=0
      case "$key" in
        # A successful single-key read with an EMPTY capture is the Enter
        # key (the newline is the read delimiter). Enter goes back one
        # level everywhere in the kit, so here it leaves the centre; a poll
        # timeout never reaches this case at all.
        '')
          msg_console_drain
          msg_console_note quit
          msg_console_clear
          printf 'Left the message centre; sp msg reopens it.\n'
          return 0
          ;;
        [0-9])
          msg_console_read_number "$key"
          msg_console_open_thread "$MSG_CONSOLE_ID" "$MSG_CONSOLE_NUMBER" \
            "$seconds" || inner=$?
          ;;
        x | X)
          # Targets outside the session manager have no terminal number;
          # their rows carry x-keys (x1, x2, …) that can never collide.
          msg_console_read_number "x"
          msg_console_open_thread "$MSG_CONSOLE_ID" "$MSG_CONSOLE_NUMBER" \
            "$seconds" || inner=$?
          ;;
        a | A)
          msg_console_followup "$seconds" || inner=$?
          ;;
        n | N)
          msg_console_new "$seconds" || inner=$?
          ;;
        l | L)
          msg_console_pick "$seconds" || inner=$?
          ;;
        r | R) ;;
        # A single-key read hands Ctrl-D over as a plain byte, never as
        # end-of-input, so the habit still has to mean "leave". It only
        # arrives at all when it is typed while this read is waiting: pressed
        # between two frames it is the terminal's own end-of-file character,
        # and the switch out of canonical mode for the next read drops it. q
        # is the key that always works.
        q | Q | $'\004')
          msg_console_drain
          msg_console_note quit
          msg_console_clear
          printf 'Left the message centre; sp msg reopens it.\n'
          return 0
          ;;
        *) ;;
      esac
      case "$inner" in
        0) ;;
        2)
          msg_console_note input_closed
          return 0
          ;;
        *) return "$inner" ;;
      esac
    else
      case "$MSG_CONSOLE_READ" in
        interrupt)
          MSG_CONSOLE_NOTICE="Ctrl-C redraws this console. Press q to leave it."
          ;;
        timeout) ;;
        *)
          msg_console_note input_closed
          return 0
          ;;
      esac
    fi
  done
}

msg_console() {
  local seconds status=0 saved_interrupt
  MSG_CONSOLE_ID=${1:-}
  MSG_CONSOLE_OFFERED_NEW=0
  seconds=$(msg_console_seconds)
  # sp's own INT trap stops the command outright. Inside the console an
  # interrupt is a redraw, exactly as it is in the picker, so the handler is
  # swapped for the length of the loop and put back afterwards.
  saved_interrupt=$(trap -p INT)
  MSG_CONSOLE_INTERRUPTED=0
  MSG_CONSOLE_INDEX=""
  MSG_CONSOLE_NOTICE=""
  MSG_CONSOLE_MSG_ID=""
  trap 'MSG_CONSOLE_INTERRUPTED=1' INT
  msg_console_note opened
  msg_console_loop "$seconds" || status=$?
  trap - INT
  [[ -z $saved_interrupt ]] || eval "$saved_interrupt"
  return "$status"
}

msg_report() {
  if (( $# > 1 )); then
    msg_usage >&2
    return 2
  fi
  local id=${1:-}
  if [[ $id == -* ]]; then
    msg_usage >&2
    return 2
  fi
  if msg_console_enabled; then
    msg_console "$id"
    return
  fi
  local payload
  payload=$(msg_fetch_report "$id") || {
    sk_die "could not read that message report"
    return 1
  }
  printf '%s' "$payload" | msg_render_report || return
  # This printed every thread it carries, replies included, so the markers it
  # showed clear here -- the same contract as the console's thread view, and
  # what keeps the picker cue from advertising a reply that is already read.
  # Only the threads actually marked unread are named: marking a thread that
  # carries no marker reaches the same state and costs a process per target.
  local key
  while IFS= read -r key; do
    msg_mark_thread_read "$key"
  done < <(printf '%s' "$payload" | msg_render unread-keys 2>/dev/null)
}

# Agents run this inside their own session; the core proves which session is
# calling. Nothing is added between them: the caller's exit status and output
# are the core's own.
msg_reply() {
  if (( $# != 2 )); then
    msg_usage >&2
    return 2
  fi
  exec python3 "$INVENTORY_CORE" msg reply "$1" --text "$2"
}

msg_list() {
  if (( $# != 0 )); then
    msg_usage >&2
    return 2
  fi
  local payload
  payload=$(python3 "$INVENTORY_CORE" msg list) || {
    sk_die "could not read the message ledger"
    return 1
  }
  printf '%s' "$payload" | msg_render_list
}
