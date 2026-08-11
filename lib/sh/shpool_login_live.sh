#!/usr/bin/env bash
# Picker process lifetime and the live snapshot loop: temporary files, the
# logged exit path, background collection, and the reads that let a menu
# repaint under a half-typed line. Source this file; do not execute it.
#
# Source order: bin/shpool_login sources this module before it installs its
# first trap, so trap cleanup EXIT always names a defined function. Everything
# else these functions read (TEMP_FILES, NEW_TEMP, SNAPSHOT, VIEW, PAGE, QUERY,
# LIVE_PID, LIVE_FILE, LIVE_DONE, LIVE_FINGERPRINT, PICKER_LIVE_STATUS,
# PENDING_CACHE, PICKER_INTERRUPTED, RECOVERY) is assigned further down that
# file, which is still before the boot sequence that first calls them.
#
# Globals the entry script owns are assigned there, not here.
# shellcheck disable=SC2154

cleanup() {
  picker_input_restore
  local path
  for path in "${TEMP_FILES[@]}"; do
    [[ -z $path || ! -f $path ]] || command rm -- "$path"
  done
}

picker_input_restore() {
  if [[ -n ${PICKER_TTY_STATE:-} && -t 0 ]]; then
    stty "$PICKER_TTY_STATE" < /dev/tty 2>/dev/null || true
  fi
  PICKER_TTY_STATE=""
}

# Every deliberate or forced picker exit records why, so "it dropped me to a
# regular terminal" is diagnosable from the action log after the fact.
picker_exit() {
  local reason=$1
  sk_log_action picker_exit "$reason" || true
  exit 2
}

picker_live_seconds() {
  local seconds=${SESSION_KIT_PICKER_REFRESH_SECONDS:-5}
  [[ $seconds =~ ^[0-9]+$ ]] || seconds=0
  # A repaint REPLACES the menu. A terminal that cannot be cleared would
  # stack copies down the screen instead, so it stays static.
  (( PICKER_SCREEN )) || seconds=0
  # Never poll faster than a snapshot takes to collect.
  (( seconds == 0 || seconds >= 2 )) || seconds=2
  printf '%s' "$seconds"
}

# What the menu actually shows. Collection timestamps are deliberately absent
# so an unchanged estate never repaints.
picker_view_fingerprint() {
  python3 - "$VIEW" "$PAGE" "$QUERY" <<'PY' 2>/dev/null || printf 'unknown'
import hashlib
import json
import sys

path, page, query = sys.argv[1:4]
try:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
except (OSError, ValueError):
    print("unreadable")
    raise SystemExit(0)
fields = (
    "display_shpool_id",
    "display_provider",
    "account_alias",
    "display_title",
    "display_color",
    "agent_status",
    "availability",
    "active_subagent_count",
    "automatic_name_state",
    "terminal_number",
)
source_rows = [
    row for row in data.get("sessions", []) if isinstance(row, dict)
]
# Terminal 1 is reserved for Fleet Supervisor. The renderer pins it before
# drawing, so fingerprint the fresh pre-render view in that same order. Without
# this normalization, every background collection looks different from the
# already-pinned screen and triggers a full-screen repaint despite identical
# visible content.
slot_one = [
    index
    for index, row in enumerate(source_rows)
    if isinstance(row.get("terminal_number"), int)
    and not isinstance(row.get("terminal_number"), bool)
    and row.get("terminal_number") == 1
]
if len(slot_one) == 1:
    supervisor = source_rows.pop(slot_one[0])
    source_rows.insert(0, supervisor)
rows = [
    [row.get(name) for name in fields]
    for row in source_rows
]
shape = {
    "rows": rows,
    "stale": data.get("stale"),
    "page": page,
    "query": query,
}
digest = hashlib.sha256(
    json.dumps(shape, sort_keys=True, default=str).encode("utf-8")
).hexdigest()
print(digest)
PY
}

picker_live_start() {
  [[ -z $LIVE_PID ]] || return 0
  new_temp login-live || return 1
  LIVE_FILE=$NEW_TEMP
  new_temp login-live-done || return 1
  LIVE_DONE=$NEW_TEMP
  : > "$LIVE_DONE"
  # An exited-but-unreaped child still answers `kill -0`, so completion is
  # published explicitly instead of inferred from the process table.
  {
    sk_timeout "${SK_REFRESH_TIMEOUT:-30}" "$STATUS_CMD" --json \
      > "$LIVE_FILE" 2>/dev/null
    printf '%s\n' "$?" > "$LIVE_DONE"
  } &
  LIVE_PID=$!
}

picker_live_ready() {
  [[ -n $LIVE_PID && -n $LIVE_DONE && -s $LIVE_DONE ]]
}

# 0 only when a fresh snapshot was adopted AND it changes the visible list.
# The active search and page are preserved: silently resetting a filter the
# human is reading would be worse than the staleness this replaces.
picker_live_collect() {
  picker_live_ready || return 1
  local status
  IFS= read -r status < "$LIVE_DONE" 2>/dev/null || status=1
  [[ $status =~ ^[0-9]+$ ]] || status=1
  wait "$LIVE_PID" 2>/dev/null || true
  LIVE_PID=
  LIVE_DONE=
  if (( status != 0 )) || [[ ! -s $LIVE_FILE ]]; then
    LIVE_FILE=
    sk_log_action picker_refresh "background_failed_status_$status" || true
    if [[ -z $LIVE_WARNING ]]; then
      LIVE_WARNING="Live refresh failed; showing the last trustworthy session list."
      return 2
    fi
    return 1
  fi
  local previous_snapshot=$SNAPSHOT previous_view=$VIEW
  SNAPSHOT=$LIVE_FILE
  LIVE_FILE=
  if ! build_view; then
    SNAPSHOT=$previous_snapshot
    VIEW=$previous_view
    sk_log_action picker_refresh background_invalid_snapshot || true
    if [[ -z $LIVE_WARNING ]]; then
      LIVE_WARNING="Live refresh returned invalid data; showing the last trustworthy session list."
      return 2
    fi
    return 1
  fi
  clamp_page
  refresh_pending_cache
  local fingerprint had_warning=0
  [[ -z $LIVE_WARNING ]] || had_warning=1
  LIVE_WARNING=""
  fingerprint=$(picker_view_fingerprint)
  [[ $fingerprint != "$LIVE_FINGERPRINT" || $had_warning == 1 ]] || return 1
  LIVE_FINGERPRINT=$fingerprint
  return 0
}

# Shared read wrapper: interrupt = redraw (rc 1), end-of-input = picker ends.
picker_read() {
  local __sk_read_var=$1 __sk_read_prompt=$2
  PICKER_INTERRUPTED=0
  # The indirection is deliberate: callers pass the DESTINATION variable name.
  # shellcheck disable=SC2229
  if IFS= read -r -p "$__sk_read_prompt" "$__sk_read_var"; then
    return 0
  fi
  if (( PICKER_INTERRUPTED )); then
    PICKER_INTERRUPTED=0
    echo
    return 1
  fi
  picker_exit input_closed
}

# Modal screens own the terminal until the person explicitly leaves them.
# A signal that interrupts a plain Bash read must not silently return Help,
# More, a project form, or a confirmation to the repainting home loop.
picker_modal_read() {
  local __sk_modal_var=$1 __sk_modal_prompt=$2
  while true; do
    if picker_read "$__sk_modal_var" "$__sk_modal_prompt"; then
      return 0
    fi
  done
}

# Filter-as-you-type, built on the search the picker already had. A `/` line
# that pauses mid-typing previews its own result; Enter on the same line runs
# the identical search it always did. Nothing else in the picker knows this
# happened: QUERY is the one filter, and it is restored the moment the line
# stops being a search.
PICKER_QUERY_BASE=""
PICKER_FILTER_ACTIVE=0

picker_filter_live_enabled() {
  [[ ${SESSION_KIT_PICKER_FILTER_LIVE:-1} != 0 ]]
}

# Show what the half-typed line would select. Returns 0 only when the visible
# list actually changed, so an unchanged view never costs a repaint.
picker_filter_preview() {
  local buffer=$1 wanted previous=$QUERY
  if [[ $buffer == /* ]]; then
    wanted=${buffer#/}
  else
    wanted=$PICKER_QUERY_BASE
  fi
  [[ $wanted != "$QUERY" ]] || return 1
  QUERY=$wanted
  PAGE=1
  if ! build_view; then
    # A preview that cannot be built changes nothing at all: the previous
    # list stays on screen and the typed line is left alone.
    QUERY=$previous
    build_view || true
    return 1
  fi
  PICKER_FILTER_ACTIVE=1
  return 0
}

# A previewed filter belongs to the line that was being typed. If that line
# turned out to be something else -- n, a number, a close -- the preview is
# undone before the command runs, so no action ever executes against a list
# the person only glanced at.
picker_filter_finish() {
  local buffer=$1
  (( PICKER_FILTER_ACTIVE )) || return 0
  [[ $buffer != /* ]] || return 0
  PICKER_FILTER_ACTIVE=0
  [[ $QUERY != "$PICKER_QUERY_BASE" ]] || return 0
  QUERY=$PICKER_QUERY_BASE
  PAGE=1
  build_view || true
}

# Read and echo one character at a time. The picker owns the visible buffer, so
# a repaint can redraw the exact half-typed command instead of erasing its echo.
picker_read_live() {
  local __sk_live_var=$1 __sk_live_prompt=$2
  local seconds buffer="" character="" read_status collect_status
  local filter_pending=0 read_timeout=1 elapsed_tenths=0
  PICKER_QUERY_BASE=$QUERY
  PICKER_FILTER_ACTIVE=0
  seconds=$(picker_live_seconds)
  if (( seconds == 0 )) || [[ ! -t 0 ]]; then
    picker_read "$__sk_live_var" "$__sk_live_prompt"
    return
  fi
  PICKER_INTERRUPTED=0
  LIVE_FINGERPRINT=$(picker_view_fingerprint)
  printf '%s' "$__sk_live_prompt"

  # Start in canonical mode. Complete lines and Ctrl-D take the ordinary path,
  # which preserves any later lines already queued for a submenu. Only after a
  # genuine one-second partial-line timeout do we take ownership of characters
  # so a repaint can reproduce them.
  # The indirection is deliberate: the caller passes the DESTINATION variable
  # name, which the character loop below fills with `printf -v`.
  # shellcheck disable=SC2229
  if IFS= read -r -t 1 "$__sk_live_var"; then
    return 0
  else
    read_status=$?
  fi
  if (( read_status <= 128 )); then
    if (( PICKER_INTERRUPTED )); then
      PICKER_INTERRUPTED=0
      echo
      return 1
    fi
    picker_exit input_closed
  fi
  elapsed_tenths=10

  PICKER_TTY_STATE=$(stty -g < /dev/tty 2>/dev/null || true)
  if [[ -z $PICKER_TTY_STATE ]] ||
     ! stty -icanon -echo min 0 time 0 < /dev/tty 2>/dev/null; then
    PICKER_TTY_STATE=""
    picker_read "$__sk_live_var" "$__sk_live_prompt"
    return
  fi
  # Characters typed during the canonical second are already visible. Drain
  # them into our buffer without echoing them a second time.
  while true; do
    character=""
    if ! IFS= read -r -s -n 1 -t 0.01 character; then
      break
    fi
    if [[ -z $character || $character == $'\r' ]]; then
      printf -v "$__sk_live_var" '%s' "$buffer"
      echo
      picker_input_restore
      picker_filter_finish "$buffer"
      return 0
    fi
    buffer+=$character
  done
  if picker_filter_live_enabled && [[ -n $buffer ]]; then
    filter_pending=1
  fi

  while true; do
    character=""
    # A line being typed as a search is answered while it is typed: short
    # waits until the person pauses, then one preview. Everything else keeps
    # the original one-second cadence, so the live refresh below is unchanged.
    read_timeout=1
    (( filter_pending == 0 )) || read_timeout=0.25
    # shellcheck disable=SC2229
    if IFS= read -r -s -n 1 -t "$read_timeout" character; then
      # `read -n 1` returns an empty value for the newline delimiter.
      if [[ -z $character || $character == $'\r' ]]; then
        printf -v "$__sk_live_var" '%s' "$buffer"
        echo
        picker_input_restore
        picker_filter_finish "$buffer"
        return 0
      fi
      case "$character" in
        $'\177'|$'\b')
          if [[ -n $buffer ]]; then
            buffer=${buffer%?}
            printf '\b \b'
            picker_filter_live_enabled && filter_pending=1
          fi
          ;;
        $'\004')
          if [[ -z $buffer ]]; then
            picker_exit input_closed
          fi
          printf -v "$__sk_live_var" '%s' "$buffer"
          echo
          picker_input_restore
          picker_filter_finish "$buffer"
          return 0
          ;;
        *)
          buffer+=$character
          printf '%s' "$character"
          picker_filter_live_enabled && filter_pending=1
          ;;
      esac
      continue
    else
      read_status=$?
    fi
    if (( read_status <= 128 )); then
      if (( PICKER_INTERRUPTED )); then
        PICKER_INTERRUPTED=0
        echo
        picker_input_restore
        return 1
      fi
      picker_input_restore
      picker_exit input_closed
    fi
    # The person stopped typing. If the line is a search, show what it
    # selects before they commit to it; the same frame batching the live
    # refresh uses keeps the redraw from flickering the half-typed line.
    if (( filter_pending )); then
      filter_pending=0
      if picker_filter_preview "$buffer"; then
        printf '\033[?2026h\033[H\033[J'
        render_main
        printf '%s%s' "$__sk_live_prompt" "$buffer"
        printf '\033[?2026l'
      fi
    fi
    if picker_live_ready; then
      picker_live_collect
      collect_status=$?
      if (( collect_status == 0 || collect_status == 2 )); then
        # Ghostty renders faster than a whole menu can be rewritten. Batch the
        # replacement as one frame so it never exposes the cleared screen, then
        # move home and erase only the active display instead of clearing the
        # terminal and its scrollback. Unsupported terminals ignore DEC mode
        # 2026 and still receive an ordinary in-place redraw.
        printf '\033[?2026h\033[H\033[J'
        render_main
        printf '%s%s' "$__sk_live_prompt" "$buffer"
        printf '\033[?2026l'
      fi
    fi
    # Counted in tenths of a second because the wait is no longer always one
    # second: a short filter wait must not make the live refresh five times
    # more frequent than the operator asked for.
    if [[ $read_timeout == 1 ]]; then
      elapsed_tenths=$(( elapsed_tenths + 10 ))
    else
      elapsed_tenths=$(( elapsed_tenths + 3 ))
    fi
    if (( elapsed_tenths >= seconds * 10 )); then
      elapsed_tenths=0
      picker_live_start || true
    fi
  done
}

new_temp() {
  local label=$1 path
  path=$(mktemp "$SK_STATE_DIR/${label}.json.XXXXXX") || return 1
  chmod 600 "$path" || {
    command rm -- "$path"
    return 1
  }
  TEMP_FILES+=("$path")
  NEW_TEMP=$path
}

refresh_snapshot() {
  local fresh
  new_temp login-snapshot || return 1
  fresh=$NEW_TEMP
  # Bounded: a jammed daemon must degrade to "previous view" messaging, not
  # hang the login window before any menu appears. kill-after covers a child
  # that ignores the TERM.
  if ! sk_timeout "${SK_REFRESH_TIMEOUT:-30}" "$STATUS_CMD" --json > "$fresh"; then
    command rm -- "$fresh"
    return 1
  fi
  SNAPSHOT=$fresh
  QUERY=""
  PAGE=1
  # A jump marker points at a row in the list it was computed from. The next
  # list is a different list, so the marker goes with the old one.
  PICKER_JUMP_NUMBER=""
  refresh_pending_cache
  build_view
}

refresh_pending_cache() {
  PENDING_CACHE=$(recovery_count 2>/dev/null || printf '0')
  [[ $PENDING_CACHE =~ ^[0-9]+$ ]] || PENDING_CACHE=0
}

# A menu window can sit open across release rollovers, silently running old
# code against new state — every fix ships and the standing windows never see
# it. Replace this picker with the current release at the next safe point.
picker_self_upgrade() {
  [[ -n ${SESSION_KIT_RELEASE_DIR:-} ]] || return 0
  local launcher="$HOME/.local/bin/shpool_login" current
  [[ -x $launcher ]] || return 0
  current=$(cd -P -- "${SESSION_KIT_ROOT:-$HOME/.local/lib/session-kit}/current" 2>/dev/null && pwd) || return 0
  [[ -n $current && $current != "$SESSION_KIT_RELEASE_DIR" ]] || return 0
  sk_log_action picker_exit release_upgrade || true
  cleanup
  exec "$launcher"
}
