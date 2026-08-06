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
  local path
  for path in "${TEMP_FILES[@]}"; do
    [[ -z $path || ! -f $path ]] || command rm -- "$path"
  done
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
  (( PICKER_STYLE )) || seconds=0
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
    "display_title",
    "display_color",
    "agent_status",
    "availability",
    "active_subagent_count",
    "automatic_name_state",
    "terminal_number",
)
rows = [
    [row.get(name) for name in fields]
    for row in data.get("sessions", [])
    if isinstance(row, dict)
]
picker = data.get("_picker") or {}
shape = {
    "rows": rows,
    "outside": len(data.get("outside_agents", [])),
    "unavailable": picker.get("unavailable_total"),
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
    return 1
  fi
  local previous_snapshot=$SNAPSHOT previous_view=$VIEW
  SNAPSHOT=$LIVE_FILE
  LIVE_FILE=
  if ! build_view; then
    SNAPSHOT=$previous_snapshot
    VIEW=$previous_view
    return 1
  fi
  clamp_page
  refresh_pending_cache
  local fingerprint
  fingerprint=$(picker_view_fingerprint)
  [[ $fingerprint != "$LIVE_FINGERPRINT" ]] || return 1
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

# A half-typed line lives in the terminal's own input queue, where this
# process cannot see it: in canonical mode FIONREAD reports 0 until Enter is
# pressed, on both Linux and macOS. So a repaint cannot be conditional on
# "is anything typed" — instead the prompt reads through readline, which
# re-echoes whatever is still queued after the screen is cleared. Nothing is
# lost and nothing needs an ioctl.
picker_read_live() {
  local __sk_live_var=$1 __sk_live_prompt=$2
  local seconds waited=0
  seconds=$(picker_live_seconds)
  if (( seconds == 0 )) || [[ ! -t 0 ]]; then
    picker_read "$__sk_live_var" "$__sk_live_prompt"
    return
  fi
  PICKER_INTERRUPTED=0
  LIVE_FINGERPRINT=$(picker_view_fingerprint)
  printf '%s' "$__sk_live_prompt"
  while true; do
    # The status MUST be captured inside the else: after a bare `if` with no
    # else clause, $? is the status of the `if` statement itself, which is 0.
    # Plain, NOT -e: readline would pull a half-typed line into its own
    # buffer and drop it when the read times out. A canonical read leaves
    # those characters in the terminal queue, where the next read collects
    # them intact.
    # shellcheck disable=SC2229
    if IFS= read -r -t 1 "$__sk_live_var"; then
      return 0
    else
      PICKER_LIVE_STATUS=$?
    fi
    if (( PICKER_LIVE_STATUS <= 128 )); then
      if (( PICKER_INTERRUPTED )); then
        PICKER_INTERRUPTED=0
        echo
        return 1
      fi
      picker_exit input_closed
    fi
    if picker_live_ready && picker_live_collect; then
      echo
      return 3
    fi
    waited=$(( waited + 1 ))
    if (( waited >= seconds )); then
      waited=0
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
