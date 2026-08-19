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
  picker_paste_disarm
  # The collector goes FIRST. It was left running on every ordinary exit --
  # picker_live_stop had exactly one caller, the self-upgrade -- so quitting
  # during a refresh left a detached status collection burning a core and
  # ~150 MB, and its last write recreated the done-marker this loop had just
  # deleted (five such files were found on disk).
  picker_live_stop
  # The event subscriber is a long-lived child of this shell. Left behind it
  # would hold a descriptor on a deleted temp file for as long as the daemon
  # runs, so it goes before the temp files it writes to.
  picker_events_stop
  local path
  for path in "${TEMP_FILES[@]}"; do
    [[ -z $path || ! -f $path ]] || command rm -- "$path"
  done
}

# ---- pasted input -----------------------------------------------------------
# A paste is data, never a queue of commands. Unarmed, a pasted block arrives
# as ordinary keystrokes and its first newline submits, so a block containing
# `k 17` closed session 17 and a blank line inside it left the picker. With
# bracketed paste armed the terminal brackets the block in these two markers,
# and everything between them is read here as ONE literal line: it fills the
# prompt, where it can be read, edited, or used as filter text, and nothing
# inside it is ever dispatched.
PICKER_PASTE_OPEN=$'\033[200~'
PICKER_PASTE_CLOSE=$'\033[201~'
PICKER_PASTE_RAW=""
PICKER_PASTE_TEXT=""
# The bytes the last escape sequence consumed after ESC, so a caller can tell
# a paste marker from an arrow key it should keep dropping.
PICKER_ESCAPE_TAIL=""
# Far more than any filter, name, or path, and small enough that assembling it
# a character at a time stays instant.
PICKER_PASTE_LIMIT=8192

# A timed read from the terminal can eat the byte it was handed: bash pulls
# the byte out of the kernel, the timer fires before the builtin finishes, and
# the jump out of the read abandons what was already consumed -- the kernel no
# longer has it and the variable never gets it. On a loaded machine the gap
# between those two moments stretches to whole scheduler slices, and about one
# keypress in three hundred dies on the timer edge (measured 2026-08-19:
# `read -n 1 -t 1` on a raw pty under one contended CPU lost the q of a q-Enter
# exactly the way CI keeps losing it). So no read that CONSUMES from the
# terminal may carry a timeout. Waiting happens against this fifo -- a
# descriptor that never has data, so its timeouts have nothing to abandon --
# and readiness is asked with `read -t 0`, which consumes nothing.
PICKER_TICK_FD=""
PICKER_TICK_FAILED=0

picker_tick_open() {
  [[ -z $PICKER_TICK_FD ]] || return 0
  (( ! PICKER_TICK_FAILED )) || return 1
  local fifo=${SK_STATE_DIR:-${TMPDIR:-/tmp}}/.picker-tick.$$
  command rm -f -- "$fifo"
  if ! mkfifo -m 600 -- "$fifo" 2>/dev/null; then
    PICKER_TICK_FAILED=1
    return 1
  fi
  # Opened read-write, so this shell is its own writer and the open cannot
  # block; unlinked at once, so the descriptor is the only trace it leaves.
  if ! exec {PICKER_TICK_FD}<>"$fifo"; then
    PICKER_TICK_FD=""
    PICKER_TICK_FAILED=1
    command rm -f -- "$fifo"
    return 1
  fi
  command rm -f -- "$fifo"
}

# Sleep without touching the terminal. Nothing ever writes the tick fifo, so
# the timeout is the whole point, and a trapped signal still ends the wait
# early -- which is how Ctrl-C keeps its sub-second response.
picker_tick() {
  local __sk_tick_junk=""
  IFS= read -r -t "${1:-0.05}" -u "$PICKER_TICK_FD" __sk_tick_junk 2>/dev/null
  return 0
}

# True when the terminal has input ready to hand over whole -- a complete line
# in canonical mode, any byte in character mode -- asked without consuming:
# a zero timeout never reads.
picker_input_ready() {
  IFS= read -r -t 0
}

# Wait up to $1 MICROSECONDS for terminal input. 0 the moment input is ready;
# 142 when the budget ran out or a trap flagged interrupt or resize, so
# callers answer signals as fast as the timed read they replaced. The budget
# is measured on the wall clock, not in ticks: under load every tick pays a
# scheduling tax, and twenty of them stretched the one-second beat to two or
# three -- which starved the event-collection cadence the beat drives.
picker_wait_input() {
  local __sk_wait_end=0 __sk_wait_ticks=0 __sk_wait_limit=0
  # Readiness is asked again AFTER every tick and answered before the signal
  # flags: keys and a Ctrl-C landing inside the same tick must come out in
  # the order the person produced them. The old blocking read had this
  # property for free -- it returned each queued byte ahead of noticing any
  # flag -- and the interrupt test types a line and interrupts it in the
  # same breath, expecting the line echoed and THEN thrown away.
  if [[ -n ${EPOCHREALTIME:-} ]]; then
    __sk_wait_end=$(( ${EPOCHREALTIME/./} + $1 ))
    while true; do
      picker_input_ready && return 0
      (( ${EPOCHREALTIME/./} < __sk_wait_end )) || return 142
      picker_tick
      picker_input_ready && return 0
      (( PICKER_INTERRUPTED == 0 && PICKER_RESIZED == 0 )) || return 142
    done
  fi
  # No sub-second clock in this bash: count ticks and accept the stretch.
  __sk_wait_limit=$(( $1 / 50000 ))
  (( __sk_wait_limit > 0 )) || __sk_wait_limit=1
  while true; do
    picker_input_ready && return 0
    (( __sk_wait_ticks < __sk_wait_limit )) || return 142
    picker_tick
    __sk_wait_ticks=$(( __sk_wait_ticks + 1 ))
    picker_input_ready && return 0
    (( PICKER_INTERRUPTED == 0 && PICKER_RESIZED == 0 )) || return 142
  done
}

picker_paste_arm() {
  (( PICKER_SCREEN )) || return 0
  [[ -t 0 ]] || return 0
  printf '\033[?2004h'
}

picker_paste_disarm() {
  (( PICKER_SCREEN )) || return 0
  [[ -t 0 ]] || return 0
  printf '\033[?2004l'
}

# One line, no control bytes. Every newline in a paste is a separator the
# prompt would have read as Enter, so they become spaces along with every
# other control byte -- nothing pasted can submit a line, move the cursor, or
# forge a row.
picker_paste_clean() {
  local text=${1//[[:cntrl:]]/ }
  local -a words=()
  # Default IFS collapses runs of blanks and drops them at both ends.
  read -r -a words <<<"$text" || true
  text="${words[*]}"
  PICKER_PASTE_TEXT=${text:0:$PICKER_PASTE_LIMIT}
}

# Read the rest of a paste straight from the terminal, up to its closing
# marker. Bounded twice: a terminal that never closes the block must not hold
# the prompt, and an enormous paste must not be assembled forever.
# One raw byte of a paste block: -N so a newline is data, the wait spent on
# ticks (forty of them, the old two-second bound), the take untimed -- the
# same separation as every other consuming read here.
picker_paste_next() {
  local __sk_paste_var=$1 __sk_paste_ticks=0
  if [[ -z $PICKER_TICK_FD ]]; then
    # shellcheck disable=SC2229
    IFS= read -r -s -N 1 -t 2 "$__sk_paste_var" 2>/dev/null  # tick-fallback
    return
  fi
  while ! picker_input_ready; do
    __sk_paste_ticks=$(( __sk_paste_ticks + 1 ))
    (( __sk_paste_ticks <= 40 )) || return 1
    picker_tick
  done
  # shellcheck disable=SC2229
  IFS= read -r -s -N 1 "$__sk_paste_var" 2>/dev/null
}

picker_paste_drain() {
  local character=""
  PICKER_PASTE_RAW=""
  while (( ${#PICKER_PASTE_RAW} < PICKER_PASTE_LIMIT )); do
    # -N takes the byte as it is, so a newline inside the block is data here
    # rather than the end of a read.
    picker_paste_next character || break
    if [[ $character == $'\033' ]]; then
      picker_swallow_escape
      [[ $PICKER_ESCAPE_TAIL != '[201~' ]] || break
      continue
    fi
    PICKER_PASTE_RAW+=$character
  done
}

# Fold a line that carries an opening marker -- plus whatever the terminal
# still has queued behind it -- into one literal line in PICKER_PASTE_TEXT.
picker_paste_absorb() {
  local line=$1 prefix body trailing=""
  prefix=${line%%"$PICKER_PASTE_OPEN"*}
  body=${line#*"$PICKER_PASTE_OPEN"}
  if [[ $body == *"$PICKER_PASTE_CLOSE"* ]]; then
    trailing=${body#*"$PICKER_PASTE_CLOSE"}
    body=${body%%"$PICKER_PASTE_CLOSE"*}
  else
    # The block runs past the newline that ended this read; the rest of it is
    # still queued and belongs to the same paste.
    picker_paste_drain
    body+=$'\n'$PICKER_PASTE_RAW
  fi
  picker_paste_clean "$prefix$body$trailing"
}

picker_paste_in_line() {
  [[ $1 == *"$PICKER_PASTE_OPEN"* ]]
}

# Ctrl-C abandons the line, and everything typed ahead of it goes with the
# line: a keystroke still sitting in the terminal is part of the command the
# person just cancelled, and reading it at the next prompt would act on it.
# A real interrupt keystroke flushes the queue itself; a signal delivered any
# other way does not, which is the whole difference this closes.
picker_discard_pending_input() {
  local __sk_junk="" __sk_reads=0
  [[ -t 0 ]] || return 0
  while (( __sk_reads < PICKER_PASTE_LIMIT )); do
    __sk_reads=$(( __sk_reads + 1 ))
    IFS= read -r -s -N 1 -t 0.01 __sk_junk 2>/dev/null || break  # discard-by-design
  done
  return 0
}

# What a person had set up on the screen, carried across a self-upgrade only.
# Read once and unset, so nothing a session is launched with ever inherits it.
# Called after the first snapshot, which is what clears the filter and page.
picker_resume_view() {
  local page=${SESSION_KIT_PICKER_RESUME_PAGE:-}
  local query=${SESSION_KIT_PICKER_RESUME_QUERY:-}
  unset SESSION_KIT_PICKER_RESUME_QUERY SESSION_KIT_PICKER_RESUME_PAGE
  if [[ -n $query ]]; then
    QUERY=$query
    build_view || {
      QUERY=""
      build_view || true
    }
  fi
  # The page is left for render_main to clamp against the size it computes.
  [[ $page =~ ^[0-9]+$ && $page -ge 1 ]] && PAGE=$page
  return 0
}

picker_input_restore() {
  if [[ -n ${PICKER_TTY_STATE:-} && -t 0 ]]; then
    stty "$PICKER_TTY_STATE" < /dev/tty 2>/dev/null || true
  fi
  PICKER_TTY_STATE=""
}

# Every deliberate or forced picker exit records why, so "it dropped me to a
# regular terminal" is diagnosable from the action log after the fact. Leaving
# on purpose is a success; anything else is the picker failing to run, and its
# reason is already on stderr (`sp help exit-codes`).
picker_exit() {
  local reason=$1 status=1
  sk_log_action picker_exit "$reason" || true
  case "$reason" in
    terminal_requested | quit | input_closed) status=0 ;;
  esac
  exit "$status"
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

# ---- push events ------------------------------------------------------------
# The list changes when a session is created, attached, detached or removed,
# and the daemon says so the moment it happens (EVENTS.md). Subscribing to that
# stream is what lets the timed poll drop from every five seconds to every
# thirty: the poll stops being how the picker learns and becomes the safety net
# under a stream that can be dropped.
#
# Three properties of the stream decide this whole design:
#   * the events carry no payload -- one line means "the table moved", so every
#     event is answered by the same full collection the poll runs;
#   * a slow subscriber is DROPPED by the daemon, with no replay, so a stream
#     that ends is not "quiet", it is a hole -- reconnecting has to be paired
#     with a full resync or the picker keeps showing what was true before it;
#   * nothing here may start a process in the typing loop, so the stream is
#     read through one long-lived file descriptor with the `read` builtin.
#
# Off with SESSION_KIT_PICKER_EVENTS=0 -- or off, no, false, in any
# capitalisation, spaces around it and all (picker_switch_off) -- which
# restores exactly the five-second poll this replaced. Watchdog health probes
# are NOT part of this: events say the table changed, never that a session is
# healthy.
PICKER_EVENTS_STATE=off
PICKER_EVENTS_FILE=
PICKER_EVENTS_DOWN=
PICKER_EVENTS_PID=
PICKER_EVENTS_FD=
PICKER_EVENTS_STARTS=0
# Two failed subscriptions are a broken stream, not a race: after that the
# picker stays on the poll for the rest of its life and says so in the log.
PICKER_EVENTS_MAX_STARTS=2
PICKER_LIVE_INTERVAL=0
# The attention watcher. The daemon's stream cannot carry "this session is
# waiting for a person" -- that is no create, no attach, no detach and no
# remove -- so on its own it is not a reason to poll less often: the one column
# an operator watches would have got six times slower. Measured on this estate:
# 25 seconds of `shpool events` over eleven busy sessions, zero lines. This
# second stream watches the files that answer changes, and the poll widens ONLY
# while both are live.
PICKER_PULSE_PID=
PICKER_PULSE_STATE=off
# An event that lands while a collection is already running describes a table
# that collection may have read BEFORE the change. Coalescing an event storm is
# right; coalescing an event into a snapshot older than itself is how a change
# disappears until the next poll.
PICKER_EVENTS_TRAILING=0

# A switch an operator reaches for is normalised before it is read. These are
# documented rollback levers on an always-on child, and `OFF`, `Off` and a
# value with a space around it are the same answer as `off`: a lever that
# silently does nothing unless it is typed in lower case is worse than no lever
# at all, because it reports success. Every switch this round added on the
# Python side already reads them this way (claude_socket.channel_enabled,
# attention.py), and `${VAR,,}` is already used in this tree.
picker_switch_off() {
  local value=$1
  value=${value#"${value%%[![:space:]]*}"}
  value=${value%"${value##*[![:space:]]}"}
  case "${value,,}" in
    0 | off | no | false) return 0 ;;
  esac
  return 1
}

picker_events_wanted() {
  ! picker_switch_off "${SESSION_KIT_PICKER_EVENTS:-auto}" || return 1
  (( PICKER_SCREEN )) || return 1
  [[ -n ${SK_SHPOOL:-} && -x ${SK_SHPOOL:-} ]] || return 1
  return 0
}

# The capability probe. `shpool events` exists from the release that added the
# events socket; an older daemon's CLI refuses the subcommand, which is the
# answer this needs and the reason it is asked of the binary rather than
# guessed from a version string.
picker_events_supported() {
  sk_timeout "${SK_EVENTS_PROBE_TIMEOUT:-5}" "$SK_SHPOOL" events --help \
    > /dev/null 2>&1
}

# Subscribe, and treat the subscription as the thing that can fail: the
# subscriber's exit is published as a marker file so the typing loop can see a
# dropped stream with a builtin test instead of reaping a process.
picker_events_start() {
  [[ -z $PICKER_EVENTS_PID ]] || return 0
  (( PICKER_EVENTS_STARTS < PICKER_EVENTS_MAX_STARTS )) || return 1
  PICKER_EVENTS_STARTS=$(( PICKER_EVENTS_STARTS + 1 ))
  new_temp login-events || return 1
  PICKER_EVENTS_FILE=$NEW_TEMP
  new_temp login-events-down || return 1
  PICKER_EVENTS_DOWN=$NEW_TEMP
  : > "$PICKER_EVENTS_FILE"
  : > "$PICKER_EVENTS_DOWN"
  {
    "$SK_SHPOOL" events >> "$PICKER_EVENTS_FILE" 2>/dev/null < /dev/null
    printf 'down\n' > "$PICKER_EVENTS_DOWN"
  } &
  PICKER_EVENTS_PID=$!
  # One descriptor for the life of the subscription. Every later read picks up
  # where the last one stopped, so no event is read twice and none is missed.
  exec {PICKER_EVENTS_FD}< "$PICKER_EVENTS_FILE" || {
    PICKER_EVENTS_FD=
    picker_events_stop
    return 1
  }
  PICKER_EVENTS_STATE=live
  picker_pulse_start || sk_log_action picker_events no_pulse || true
  sk_log_action picker_events subscribed || true
  return 0
}

# How often the attention watcher looks, validated before it is interpolated
# into the child's argv. `pulse.py --interval abc` exits 2 inside argparse
# before it watches anything, so an unparseable value KILLED the watcher in
# milliseconds -- and the picker widened its poll anyway, which is precisely
# the widened-and-blind state the round promises can never happen. The sibling
# SESSION_KIT_PICKER_EVENT_POLL_SECONDS is validated the same way in
# picker_live_interval; a typo falls back to the default rather than to
# nothing. The Python side clamps the number it is given (0.05-60s), so the
# only class this has to catch is "argparse cannot parse it at all".
PICKER_PULSE_DEFAULT_SECONDS=1
picker_pulse_interval() {
  local value=${SESSION_KIT_PICKER_PULSE_SECONDS:-}
  value=${value#"${value%%[![:space:]]*}"}
  value=${value%"${value##*[![:space:]]}"}
  [[ $value =~ ^([0-9]+|[0-9]*\.[0-9]+)$ ]] || value=$PICKER_PULSE_DEFAULT_SECONDS
  printf '%s' "$value"
}

# Is the attention watcher still there? A background child that has exited is
# still a pid until the shell reaps it, so `kill -0` answers yes for a watcher
# that died a millisecond ago -- the state field is what tells them apart.
# pulse.py reads its own parent exactly this way (_parent_is_zombie).
picker_pulse_alive() {
  [[ -n $PICKER_PULSE_PID ]] || return 1
  kill -0 "$PICKER_PULSE_PID" 2>/dev/null || return 1
  local stat_line=
  if [[ -r /proc/$PICKER_PULSE_PID/stat ]]; then
    read -r stat_line < "/proc/$PICKER_PULSE_PID/stat" || return 0
    # The comm field is parenthesised and may contain spaces; the state is the
    # first field after the last ')'.
    stat_line=${stat_line##*') '}
    [[ ${stat_line:0:1} != Z ]] || return 1
  else
    # No /proc (macOS): ask ps for the same letter. One subprocess, and only
    # on the platform that has no cheaper answer.
    local state=
    state=$(ps -o state= -p "$PICKER_PULSE_PID" 2>/dev/null) || state=
    state=${state//[[:space:]]/}
    [[ ${state:0:1} != Z ]] || return 1
  fi
  return 0
}

# One line per change, into the same file the daemon's events go to: the typing
# loop drains one descriptor and does not care which stream said so.
picker_pulse_start() {
  [[ -z $PICKER_PULSE_PID ]] || return 0
  ! picker_switch_off "${SESSION_KIT_PICKER_PULSE:-auto}" || return 1
  [[ -n $PICKER_EVENTS_FILE && -f $PICKER_EVENTS_FILE ]] || return 1
  local tool=${SESSION_KIT_PULSE_TOOL:-"$SCRIPT_DIR/../lib/sessionkit_inventory/pulse.py"}
  [[ -f $tool ]] || return 1
  local interval
  interval=$(picker_pulse_interval)
  # --parent-pid is this picker. picker_pulse_stop is the ordinary stop; this
  # is the one that survives a picker nobody gets to trap -- an OOM kill, a
  # SIGKILL -- after which nothing else would ever end this child.
  python3 "$tool" --interval "$interval" \
    --parent-pid "$$" \
    >> "$PICKER_EVENTS_FILE" 2>/dev/null < /dev/null &
  PICKER_PULSE_PID=$!
  # A watcher that is already gone is not a watcher. The poll may only widen
  # behind one that is running, so the state says live only if it is.
  if ! picker_pulse_alive; then
    picker_pulse_stop
    return 1
  fi
  PICKER_PULSE_STATE=live
  return 0
}

picker_pulse_stop() {
  [[ -n $PICKER_PULSE_PID ]] || return 0
  kill "$PICKER_PULSE_PID" 2>/dev/null || true
  wait "$PICKER_PULSE_PID" 2>/dev/null || true
  PICKER_PULSE_PID=
  PICKER_PULSE_STATE=off
}

picker_events_stop() {
  picker_pulse_stop
  if [[ -n $PICKER_EVENTS_FD ]]; then
    # The braces are load-bearing. A redirection written on a bare `exec`
    # applies to the SHELL and never comes back, so `exec {FD}<&- 2>/dev/null`
    # closed the subscription AND pointed this picker's stderr at /dev/null
    # for the rest of its life. Nothing looked wrong -- the picker still drew,
    # still took keys -- but shpool's client declines to put a terminal into
    # raw mode unless stdin, stdout AND stderr are all a terminal
    # (libshpool/src/tty.rs, set_attach_flags), and it declines SILENTLY. So
    # every session opened from that picker afterwards came up with the
    # keyboard dead: keystrokes echoed by the kernel as literal ^L over the
    # provider's first screen, nothing delivered until Enter. The operator hit
    # exactly this at 00:46 on 2026-08-15, twelve minutes of it, and the only
    # cure was `stty raw -echo` typed from another window.
    #
    # Grouping keeps the suppression where it was meant to be -- on this one
    # close, which is all it was ever written to quieten -- and leaves fd 2
    # alone. The close still happens and a bad descriptor is still silent;
    # tests/test_picker_stderr_survives.py holds all three properties.
    { exec {PICKER_EVENTS_FD}<&-; } 2>/dev/null || true
    PICKER_EVENTS_FD=
  fi
  if [[ -n $PICKER_EVENTS_PID ]]; then
    # The subscriber is a subshell whose child is the long-lived `shpool
    # events` run. Killing the subshell alone would leave that run attached to
    # the daemon, writing into a file this function is about to unlink -- the
    # same detached-collector shape picker_live_stop was written for.
    local child
    for child in $(pgrep -P "$PICKER_EVENTS_PID" 2>/dev/null || true); do
      kill "$child" 2>/dev/null || true
    done
    kill "$PICKER_EVENTS_PID" 2>/dev/null || true
    wait "$PICKER_EVENTS_PID" 2>/dev/null || true
    PICKER_EVENTS_PID=
  fi
  [[ -z $PICKER_EVENTS_FILE ]] || command rm -f -- "$PICKER_EVENTS_FILE"
  PICKER_EVENTS_FILE=
  [[ -z $PICKER_EVENTS_DOWN ]] || command rm -f -- "$PICKER_EVENTS_DOWN"
  PICKER_EVENTS_DOWN=
}

# 0 when the estate should be re-collected NOW: either an event arrived, or the
# stream died and everything it would have said is unknown. Builtins only.
picker_events_pending() {
  [[ $PICKER_EVENTS_STATE == live ]] || return 1
  local line seen=1
  if [[ -n $PICKER_EVENTS_FD ]]; then
    # A line half-written when this read lands is still evidence that the
    # table moved; a duplicate refresh is free, because a collection already
    # in flight is not started twice.
    while IFS= read -r -t 0.01 -u "$PICKER_EVENTS_FD" line || [[ -n $line ]]; do
      [[ -z $line ]] || seen=0
      line=""
    done
  fi
  # The daemon drops subscribers that fall behind and never replays what they
  # missed, so a stream that ended is a resync, not a quiet estate.
  if [[ -n $PICKER_EVENTS_DOWN && -s $PICKER_EVENTS_DOWN ]]; then
    sk_log_action picker_events dropped || true
    picker_events_stop
    if picker_events_start; then
      sk_log_action picker_events resubscribed_resync || true
    else
      PICKER_EVENTS_STATE=fallback
      sk_log_action picker_events fallback_poll || true
    fi
    return 0
  fi
  return "$seen"
}

# Collect now, for a reason that arrived from outside: an event, an attention
# change, or a resync after a dropped stream. If a collection is already
# running it may have read the session table BEFORE this reason existed, so the
# request is remembered and answered the moment that one is adopted. The timed
# poll deliberately does NOT do this -- it has no news, and chaining it would
# turn a slow estate into one continuous collection.
picker_collect_now() {
  if [[ -n $LIVE_PID ]]; then
    PICKER_EVENTS_TRAILING=1
    return 0
  fi
  picker_live_start || true
}

# Subscribe once per picker, and resync immediately after: the estate can have
# moved between the snapshot on screen and the first event this stream carries.
picker_events_open() {
  [[ $PICKER_EVENTS_STATE == off ]] || return 1
  picker_events_wanted || return 1
  if ! picker_events_supported; then
    PICKER_EVENTS_STATE=fallback
    sk_log_action picker_events unsupported || true
    return 1
  fi
  picker_events_start || {
    PICKER_EVENTS_STATE=fallback
    sk_log_action picker_events start_failed || true
    return 1
  }
  return 0
}

# How long the timed collection may wait. With a live stream the poll is the
# net under it, not the way the picker learns; without one it is the whole
# mechanism and keeps its old cadence.
picker_live_interval() {
  local base=$1 widened
  PICKER_LIVE_INTERVAL=$base
  [[ $PICKER_EVENTS_STATE == live ]] || return 0
  # Without the attention watcher the timed poll is still the only way a
  # waiting session is noticed, so it keeps the cadence it always had. This is
  # the whole guarantee: the poll never widens past a stream that cannot carry
  # attention.
  [[ $PICKER_PULSE_STATE == live ]] || return 0
  # Asked at the moment of widening, not once at start-up: a watcher that died
  # for any reason -- a bad interval, a missing interpreter, an OOM kill --
  # would otherwise leave this poll six times slower with nothing pushing
  # attention changes into it, and nothing on screen or in the log to say so.
  if ! picker_pulse_alive; then
    picker_pulse_stop
    sk_log_action picker_events pulse_down || true
    return 0
  fi
  widened=${SESSION_KIT_PICKER_EVENT_POLL_SECONDS:-30}
  [[ $widened =~ ^[0-9]+$ ]] || widened=30
  (( widened >= base )) || widened=$base
  PICKER_LIVE_INTERVAL=$widened
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
  new_temp login-live-pending || return 1
  LIVE_PENDING=$NEW_TEMP
  new_temp login-live-done || return 1
  LIVE_DONE=$NEW_TEMP
  : > "$LIVE_DONE"
  # An exited-but-unreaped child still answers `kill -0`, so completion is
  # published explicitly instead of inferred from the process table.
  #
  # The pending-recovery count is collected here too, as ONE integer in a file.
  # It used to be fetched between a keystroke and its echo -- a whole
  # shpool_status process, in the foreground of the typing loop, in -echo mode,
  # for a number this same collector already holds. That was 150 ms of every
  # five seconds in which characters the operator typed were invisible.
  {
    local status=0
    sk_timeout "${SK_REFRESH_TIMEOUT:-30}" "$STATUS_CMD" --json \
      > "$LIVE_FILE" 2>/dev/null || status=$?
    recovery_count > "$LIVE_PENDING" 2>/dev/null || : > "$LIVE_PENDING"
    printf '%s\n' "$status" > "$LIVE_DONE"
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
  local status finished_marker=$LIVE_DONE finished_pending=$LIVE_PENDING
  IFS= read -r status < "$LIVE_DONE" 2>/dev/null || status=1
  [[ $status =~ ^[0-9]+$ ]] || status=1
  wait "$LIVE_PID" 2>/dev/null || true
  LIVE_PID=
  LIVE_DONE=
  LIVE_PENDING=
  # Superseded temps are freed the moment they stop being referenced. The
  # EXIT trap remains the backstop, but a picker that dies uncleanly must
  # leak minutes of files, not days (the reaper found 36k of them).
  [[ -z $finished_marker ]] || command rm -f -- "$finished_marker"
  # The count the collector produced, read as a number and never recomputed
  # here: this is the typing loop, and nothing in it may start a process.
  if [[ -n $finished_pending ]]; then
    picker_adopt_pending "$finished_pending"
    command rm -f -- "$finished_pending"
  fi
  if (( status != 0 )) || [[ ! -s $LIVE_FILE ]]; then
    [[ -z $LIVE_FILE ]] || command rm -f -- "$LIVE_FILE"
    LIVE_FILE=
    sk_log_action picker_refresh "background_failed_status_$status" || true
    if [[ -z $LIVE_WARNING ]]; then
      LIVE_WARNING="Live refresh failed. Showing the last confirmed list."
      return 2
    fi
    return 1
  fi
  local previous_snapshot=$SNAPSHOT previous_view=$VIEW
  SNAPSHOT=$LIVE_FILE
  LIVE_FILE=
  if ! build_view; then
    [[ -z $SNAPSHOT || $SNAPSHOT == "$previous_snapshot" ]] ||
      command rm -f -- "$SNAPSHOT"
    SNAPSHOT=$previous_snapshot
    VIEW=$previous_view
    sk_log_action picker_refresh background_invalid_snapshot || true
    if [[ -z $LIVE_WARNING ]]; then
      LIVE_WARNING="Live refresh returned bad data. Showing the last confirmed list."
      return 2
    fi
    return 1
  fi
  [[ -z $previous_snapshot || $previous_snapshot == "$SNAPSHOT" ]] ||
    command rm -f -- "$previous_snapshot"
  [[ -z $previous_view || $previous_view == "$VIEW" ]] ||
    command rm -f -- "$previous_view"
  clamp_page
  local fingerprint had_warning=0
  [[ -z $LIVE_WARNING ]] || had_warning=1
  LIVE_WARNING=""
  fingerprint=$(picker_view_fingerprint)
  [[ $fingerprint != "$LIVE_FINGERPRINT" || $had_warning == 1 ]] || return 1
  LIVE_FINGERPRINT=$fingerprint
  return 0
}

# The one read every picker prompt is built on, and the only place the three
# ways a read can end are told apart:
#
#   0  a line was typed
#   1  the read was interrupted (Ctrl-C)
#   2  input ended (Ctrl-D on an empty line, or a closed pipe)
#
# What each of those MEANS belongs to the caller: the home menu leaves on 2,
# a modal goes back exactly one level. Deciding that here is what let a
# Ctrl-D three menus deep end the whole picker.
picker_read_raw() {
  local __sk_raw_var=$1 __sk_raw_prompt=$2 __sk_raw_surface=${3:-modal}
  local __sk_raw_status=0
  PICKER_INTERRUPTED=0
  # Off a terminal there is no Ctrl-C to answer and no prompt to print: a
  # script feeding this picker gets the plain blocking read it always got,
  # which is also the only read that can never split a slowly-arriving line.
  if [[ ! -t 0 ]]; then
    # The indirection is deliberate: callers pass the DESTINATION variable name.
    # shellcheck disable=SC2229
    if IFS= read -r "$__sk_raw_var"; then
      return 0
    fi
    (( PICKER_INTERRUPTED )) || return 2
    PICKER_INTERRUPTED=0
    echo
    return 1
  fi
  # A plain `read` is RESTARTED after a trapped signal, bash runs the handler
  # and goes straight back to waiting, so the interrupt flag set by the trap
  # is never looked at until a whole line is typed. That is the entire reason
  # Ctrl-C could not cancel a single prompt in this picker. A one-second bound
  # gives the flag somewhere to be noticed; the line discipline is holding any
  # half-typed line, so nothing typed is lost across the poll.
  picker_paste_arm
  printf '%s' "$__sk_raw_prompt"
  picker_tick_open || true
  while true; do
    # Waiting and consuming are separated on purpose: only an UNTIMED read may
    # take bytes off the terminal, and it runs solely after `read -t 0` says a
    # whole answer is already queued -- so it returns at once and no timer can
    # fire while it holds a half-taken keypress.
    if picker_input_ready; then
      # shellcheck disable=SC2229
      if IFS= read -r "$__sk_raw_var"; then
        # A pasted block is one answer to this prompt, never a queue of them:
        # the rest of the block is read here and folded into the same line, so
        # no later prompt is answered by a line nobody typed.
        if picker_paste_in_line "${!__sk_raw_var}"; then
          picker_paste_absorb "${!__sk_raw_var}"
          # shellcheck disable=SC2229
          printf -v "$__sk_raw_var" '%s' "$PICKER_PASTE_TEXT"
        fi
        return 0
      fi
      # Readable but nothing deliverable is closed input, not a timeout.
      __sk_raw_status=1
    elif [[ -n $PICKER_TICK_FD ]]; then
      # One wall-clock second, same beat as the timed read this replaces.
      # Input arriving mid-wait loops straight back to the take above.
      __sk_raw_status=0
      picker_wait_input 1000000 || __sk_raw_status=$?
      if (( __sk_raw_status == 0 )); then
        continue
      fi
    else
      # No fifo could be opened, so this is the old timed read -- and with it
      # the timer race everything above exists to remove.
      # The status MUST be captured in the else. After a bare `if` whose
      # condition failed, $? is the status of the `if` itself -- zero -- and
      # every one-second poll would read as closed input and walk out of the
      # prompt on its own. The console learned this the same way.
      # shellcheck disable=SC2229
      if IFS= read -r -t 1 "$__sk_raw_var"; then  # tick-fallback
        if picker_paste_in_line "${!__sk_raw_var}"; then
          picker_paste_absorb "${!__sk_raw_var}"
          # shellcheck disable=SC2229
          printf -v "$__sk_raw_var" '%s' "$PICKER_PASTE_TEXT"
        fi
        return 0
      else
        __sk_raw_status=$?
      fi
    fi
    if (( PICKER_INTERRUPTED )); then
      PICKER_INTERRUPTED=0
      echo
      return 1
    fi
    # Over 128 is the timeout; anything else is input that has closed.
    (( __sk_raw_status > 128 )) || return 2
    # A home menu with automatic repaint disabled still has this canonical
    # one-second beat. Keep upgrade detection alive here without putting it in
    # picker_modal_read, where a reload would interrupt an open prompt.
    if [[ $__sk_raw_surface == home ]] && ! picker_self_upgrade; then
      printf '\n%s\n%s' "$LIVE_WARNING" "$__sk_raw_prompt"
      LIVE_WARNING=
    fi
  done
}

# An escape sequence is ONE keystroke, not a handful of characters to type
# into a name. Arrows, Home, Delete and Page Up all arrive as ESC [ … final
# byte, and every prompt in this picker takes a number, a key, a word or a
# path -- none of which can contain one. Read the whole sequence and drop it.
# One byte of an escape sequence, waited for off-terminal and taken untimed.
# The 50ms budget is the old one: past it the sequence is over, or was never a
# sequence at all.
picker_escape_next() {
  local __sk_esc_var=$1
  if [[ -z $PICKER_TICK_FD ]]; then
    # shellcheck disable=SC2229
    IFS= read -r -s -n 1 -t 0.05 "$__sk_esc_var" 2>/dev/null  # tick-fallback
    return
  fi
  if ! picker_input_ready; then
    picker_tick 0.05
    picker_input_ready || return 1
  fi
  # shellcheck disable=SC2229
  IFS= read -r -s -n 1 "$__sk_esc_var" 2>/dev/null
}

picker_swallow_escape() {
  local extra="" code
  PICKER_ESCAPE_TAIL=""
  # A lone ESC is a key of its own; only [ and O introduce a sequence.
  picker_escape_next extra || return 0
  [[ $extra == '[' || $extra == O ]] || return 0
  PICKER_ESCAPE_TAIL=$extra
  while picker_escape_next extra; do
    [[ -n $extra ]] || continue
    PICKER_ESCAPE_TAIL+=$extra
    # Parameter and intermediate bytes (32 through 63) continue the sequence;
    # the first byte from @ to ~ ends it and is swallowed with it.
    printf -v code '%d' "'$extra" 2>/dev/null || break
    (( code >= 32 && code <= 63 )) || break
  done
}

# ESC[200~ is a paste beginning, not a key to drop. Answered wherever the
# picker reads characters, so no read anywhere treats pasted lines as input.
picker_escape_was_paste() {
  [[ $PICKER_ESCAPE_TAIL == '[200~' ]]
}

# No exit from this surface is allowed to be silent: "it just dropped me to a
# shell" has to be answerable from the screen as well as from the action log.
picker_eof_notice() {
  echo
  echo "  Leaving the picker."
}

# Home-menu read: interrupt = redraw (rc 1), end-of-input = the picker ends,
# saying so first.
picker_read() {
  local __sk_read_status=0
  picker_read_raw "$1" "$2" home || __sk_read_status=$?
  case "$__sk_read_status" in
    0) return 0 ;;
    1) return 1 ;;
  esac
  picker_eof_notice
  picker_exit input_closed
}

# Modal screens own the terminal until the person leaves them, and every way
# of leaving one is worth exactly one level. Ctrl-C cancels the prompt back to
# the menu that asked it; Ctrl-D ends the prompt the same way instead of
# killing a picker the person is three menus deep in. Both return non-zero, so
# the `|| return` guarding every call site is the live cancel path it has
# always looked like.
picker_modal_read() {
  local __sk_modal_status=0
  while true; do
    __sk_modal_status=0
    picker_read_raw "$1" "$2" || __sk_modal_status=$?
    case "$__sk_modal_status" in
      0)
        # These prompts read whole lines, so an arrow key arrives as the
        # bytes it sends -- ^[[A inside a session name, a project path, or a
        # confirmation word. The line is not an answer somebody typed; say so
        # and ask again rather than acting on what is left of it.
        if [[ ${!1} == *$'\033'* ]]; then
          echo "  Arrow keys do nothing here."
          continue
        fi
        return 0
        ;;
      1)
        echo "  Nothing changed."
        return 1
        ;;
    esac
    echo
    echo "  Back."
    return 1
  done
}

# A read-only screen whose only exit is Back. It used to accept any line and
# drop it, so a key typed there -- the number of a row that is on the screen,
# or a command meant for the list -- vanished without a word, and the person
# went back believing it had been taken.
picker_back_read() {
  local __sk_back_prompt=${1:-"  ↵ back ❯ "} __sk_back_answer
  while true; do
    picker_modal_read __sk_back_answer "$__sk_back_prompt" || return 0
    case "${__sk_back_answer,,}" in
      ""|b|back|q|quit) return 0 ;;
      *) echo "  There is nothing to choose on this screen. ↵ goes back." ;;
    esac
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
  local seconds buffer="" character="" read_status=0 collect_status
  local filter_pending=0 read_timeout=1 elapsed_tenths=0 paste_seen=0
  local wait_micros=1000000
  PICKER_QUERY_BASE=$QUERY
  PICKER_FILTER_ACTIVE=0
  # The page on the screen is the page this answer is about. A live repaint
  # between here and Enter sets this again, and a mass close then refuses
  # rather than acting on a set the person never read.
  PAGE_RENDER_CHANGED=0
  seconds=$(picker_live_seconds)
  if (( seconds == 0 )) || [[ ! -t 0 ]]; then
    picker_read "$__sk_live_var" "$__sk_live_prompt"
    return
  fi
  # Subscribing costs one probe and one collection, once per picker. The
  # collection is the resync that closes the window between the snapshot on
  # screen and the first event the stream can carry.
  if picker_events_open; then
    picker_collect_now
  fi
  PICKER_INTERRUPTED=0
  LIVE_FINGERPRINT=$(picker_view_fingerprint)
  picker_paste_arm
  printf '%s' "$__sk_live_prompt"

  # Start in canonical mode. Complete lines and Ctrl-D take the ordinary path,
  # which preserves any later lines already queued for a submenu. Only after a
  # genuine one-second partial-line timeout do we take ownership of characters
  # so a repaint can reproduce them.
  # The indirection is deliberate: the caller passes the DESTINATION variable
  # name, which the character loop below fills with `printf -v`.
  # The second itself is spent on ticks, and the take is untimed, for the
  # reason at the tick fifo's definition: a timed read that is handed a line
  # as its timer fires abandons it.
  picker_tick_open || true
  if [[ -n $PICKER_TICK_FD ]]; then
    read_status=0
    picker_wait_input 1000000 || read_status=$?
    if (( read_status == 0 )); then
      # shellcheck disable=SC2229
      if IFS= read -r "$__sk_live_var"; then
        # A paste whose first newline arrived inside the canonical second: the
        # marker is on this line and the rest of the block is still queued.
        # Take the terminal and read the whole block as data instead of
        # returning a command the person never typed.
        picker_paste_in_line "${!__sk_live_var}" || return 0
        paste_seen=1
      else
        read_status=1
      fi
    fi
  else
    # shellcheck disable=SC2229
    if IFS= read -r -t 1 "$__sk_live_var"; then  # tick-fallback
      picker_paste_in_line "${!__sk_live_var}" || return 0
      paste_seen=1
    else
      read_status=$?
    fi
  fi
  # The interrupt is answered BEFORE the status is classified. A read that a
  # trapped signal ended returns OVER 128 -- the same range as an ordinary
  # timeout -- so a flag examined only inside the `<= 128` branch is never
  # examined at all, which is how Ctrl-C came to do nothing here while the
  # same check in picker_read_raw worked. One condition, one order, both
  # readers.
  if (( paste_seen == 0 )); then
    if (( PICKER_RESIZED )) && (( PICKER_INTERRUPTED == 0 )) &&
       (( read_status > 128 )); then
      PICKER_RESIZED=0
      return 1
    fi
    if (( PICKER_INTERRUPTED )); then
      PICKER_INTERRUPTED=0
      picker_discard_pending_input
      echo
      return 1
    fi
    if (( read_status <= 128 )); then
      picker_eof_notice
      picker_exit input_closed
    fi
    # This one-second timeout is the idle home-menu beat. The outer menu loop
    # does not regain control while nobody types, so checking only there left
    # an already-open picker pinned to its old release forever. A modal uses
    # picker_modal_read instead and can never reach this check.
    if ! picker_self_upgrade; then
      picker_redraw_home "$__sk_live_prompt" ""
    fi
  fi
  elapsed_tenths=10

  PICKER_TTY_STATE=$(stty -g < /dev/tty 2>/dev/null || true)
  if [[ -z $PICKER_TTY_STATE ]] ||
     ! stty -icanon -echo min 0 time 0 < /dev/tty 2>/dev/null; then
    PICKER_TTY_STATE=""
    if (( paste_seen )); then
      # No character loop to hold the block in, so it becomes one literal line
      # instead: its newlines still cannot run as separate commands.
      picker_paste_absorb "${!__sk_live_var}"
      # shellcheck disable=SC2229
      printf -v "$__sk_live_var" '%s' "$PICKER_PASTE_TEXT"
      return 0
    fi
    picker_read "$__sk_live_var" "$__sk_live_prompt"
    return
  fi
  if (( paste_seen )); then
    # The whole block becomes the line at the prompt, where it can be read,
    # edited, or used as filter text. A paste is never acted on by itself.
    picker_paste_absorb "${!__sk_live_var}"
    buffer=$PICKER_PASTE_TEXT
    # shellcheck disable=SC2229
    printf -v "$__sk_live_var" '%s' ""
    picker_redraw_home "$__sk_live_prompt" "$buffer"
  fi
  # Characters typed during the canonical second are already visible. Drain
  # them into our buffer without echoing them a second time. Ready-check
  # before every take, one 10ms grace tick for a burst still in flight, and
  # the takes untimed: the drain must not carry the timer race the reads
  # above just gave up.
  while true; do
    character=""
    if ! picker_input_ready; then
      [[ -z $PICKER_TICK_FD ]] || picker_tick 0.01
      picker_input_ready || break
    fi
    if ! IFS= read -r -s -n 1 character; then
      break
    fi
    if [[ -z $character || $character == $'\r' ]]; then
      printf -v "$__sk_live_var" '%s' "$buffer"
      echo
      picker_input_restore
      picker_filter_finish "$buffer"
      return 0
    fi
    if [[ $character == $'\033' ]]; then
      picker_swallow_escape
      if picker_escape_was_paste; then
        picker_paste_drain
        picker_paste_clean "$PICKER_PASTE_RAW"
        buffer+=$PICKER_PASTE_TEXT
        picker_redraw_home "$__sk_live_prompt" "$buffer"
      fi
      continue
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
    # The same wait/take separation as the canonical read above: the wait is
    # off-terminal, the take is untimed and runs only against queued input.
    if [[ -n $PICKER_TICK_FD ]]; then
      wait_micros=1000000
      [[ $read_timeout == 1 ]] || wait_micros=250000
      read_status=0
      picker_wait_input "$wait_micros" || read_status=$?
      if (( read_status == 0 )) && ! IFS= read -r -s -n 1 character; then
        read_status=1
      fi
    else
      # shellcheck disable=SC2229
      if IFS= read -r -s -n 1 -t "$read_timeout" character; then  # tick-fallback
        read_status=0
      else
        read_status=$?
      fi
    fi
    if (( read_status == 0 )); then
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
        $'\033')
          # Swallowed whole, and quietly: a key that does nothing to a line
          # being typed is not an exit, and a notice printed into a half-typed
          # command would corrupt the line it is complaining about. A paste
          # beginning is the one sequence that is not a key: its whole block
          # joins the line being typed as literal text.
          picker_swallow_escape
          if picker_escape_was_paste; then
            picker_paste_drain
            picker_paste_clean "$PICKER_PASTE_RAW"
            buffer+=$PICKER_PASTE_TEXT
            picker_filter_live_enabled && filter_pending=1
            picker_redraw_home "$__sk_live_prompt" "$buffer"
          fi
          ;;
        $'\004')
          if [[ -z $buffer ]]; then
            picker_input_restore
            picker_eof_notice
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
    fi
    # Same order as the read above and as picker_read_raw: the interrupt is
    # answered first, and the line it interrupted is gone -- what is on screen
    # after a Ctrl-C is a fresh prompt, not the command the person abandoned.
    if (( PICKER_INTERRUPTED )); then
      PICKER_INTERRUPTED=0
      buffer=""
      printf -v "$__sk_live_var" '%s' ""
      picker_discard_pending_input
      echo
      picker_input_restore
      picker_filter_finish ""
      return 1
    fi
    if (( read_status <= 128 )); then
      picker_input_restore
      picker_eof_notice
      picker_exit input_closed
    fi
    # In character mode this is the same idle beat, including the pause after
    # a partially typed home-menu command. Restore/redraw only when a broken
    # release produced a new warning; a successful upgrade never returns.
    if ! picker_self_upgrade; then
      picker_redraw_home "$__sk_live_prompt" "$buffer"
    fi
    # The person stopped typing. If the line is a search, show what it
    # selects before they commit to it; the same frame batching the live
    # refresh uses keeps the redraw from flickering the half-typed line.
    if (( filter_pending )); then
      filter_pending=0
      if picker_filter_preview "$buffer"; then
        picker_redraw_home "$__sk_live_prompt" "$buffer"
      fi
    fi
    # The window changed shape. Nothing about the estate changed, so no
    # fingerprint can see it: the repaint is the answer to the signal itself.
    if (( PICKER_RESIZED )); then
      PICKER_RESIZED=0
      picker_redraw_home "$__sk_live_prompt" "$buffer"
    fi
    if picker_live_ready; then
      picker_live_collect
      collect_status=$?
      if (( collect_status == 0 || collect_status == 2 )); then
        picker_redraw_home "$__sk_live_prompt" "$buffer"
      fi
    fi
    # The daemon said the session table moved. Collect now and restart the
    # clock, so a change the operator caused is on screen in the time one
    # collection takes rather than at the end of the next poll window.
    if picker_events_pending; then
      elapsed_tenths=0
      picker_collect_now
    fi
    # Something asked for a collection while the last one was in flight. Now
    # that it has been adopted, answer it.
    if (( PICKER_EVENTS_TRAILING )) && [[ -z $LIVE_PID ]]; then
      PICKER_EVENTS_TRAILING=0
      elapsed_tenths=0
      picker_live_start || true
    fi
    # Counted in tenths of a second because the wait is no longer always one
    # second: a short filter wait must not make the live refresh five times
    # more frequent than the operator asked for.
    if [[ $read_timeout == 1 ]]; then
      elapsed_tenths=$(( elapsed_tenths + 10 ))
    else
      elapsed_tenths=$(( elapsed_tenths + 3 ))
    fi
    picker_live_interval "$seconds"
    if (( elapsed_tenths >= PICKER_LIVE_INTERVAL * 10 )); then
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

# Compute-first home frame: the whole screen is rendered into the capture
# file (in this shell, so renderer state survives), then reaches the
# terminal as one write via picker_frame_emit_file, the screen is never
# erased ahead of content, so a slow render cannot flash a blank frame.
# Falls back to the erase-first path only when the capture file is missing.
picker_redraw_home() {
  local prompt=$1 buffer=$2 cols= rows=
  if [[ -n ${PICKER_FRAME_FILE:-} && -f ${PICKER_FRAME_FILE:-} ]]; then
    # With stdout captured to a file, size probes that ioctl stdout return
    # their fallback and rows lay out for the wrong terminal. tput still
    # reads the real size from stderr, so pin it for the capture, freshly
    # each frame, which is also what keeps a resize honest.
    # Kernel winsize first (0 means never sized), tput second, exported
    # size last, same chain and reasons as the picker's own frame capture.
    __sk_size=$(command stty size <&2 2>/dev/null) || __sk_size=
    cols=${__sk_size##* }; rows=${__sk_size%% *}
    [[ $cols =~ ^[0-9]+$ && $cols -gt 0 ]] || cols=
    [[ $rows =~ ^[0-9]+$ && $rows -gt 0 ]] || rows=
    if [[ -z $cols ]]; then
      cols=$(tput cols 2>/dev/null) && [[ $cols =~ ^[0-9]+$ ]] || cols=
    fi
    if [[ -z $rows ]]; then
      rows=$(tput lines 2>/dev/null) && [[ $rows =~ ^[0-9]+$ ]] || rows=
    fi
    { COLUMNS=${cols:-${COLUMNS:-}} LINES=${rows:-${LINES:-}} render_main
      printf '%s%s' "$prompt" "$buffer"; } > "$PICKER_FRAME_FILE"
    picker_frame_emit_file "$PICKER_FRAME_FILE" home
  else
    printf '\033[?2026h\033[H\033[J'
    render_main
    printf '%s%s' "$prompt" "$buffer"
    printf '\033[?2026l'
  fi
}

# Collect a fresh snapshot. With `keep`, the search, the page and the jump
# marker survive it: an action is not a request to change what the person was
# looking at, and returning them to page 1 of the unfiltered list mid-task is
# the same defect the background collector already refuses to commit. Without
# it -- boot, and the `r` key, which says it clears the filter -- the view
# controls reset.
refresh_snapshot() {
  local mode=${1:-reset} fresh
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
  if [[ $mode != keep ]]; then
    QUERY=""
    PAGE=1
    # A jump marker points at a row in the list it was computed from. The next
    # list is a different list, so the marker goes with the old one.
    PICKER_JUMP_NUMBER=""
    refresh_pending_cache
    build_view
    return
  fi
  refresh_pending_cache
  # A filter that no longer builds is not worth losing the list over.
  build_view || {
    QUERY=""
    PAGE=1
    PICKER_JUMP_NUMBER=""
    build_view || return 1
  }
  clamp_page
}

refresh_pending_cache() {
  PENDING_CACHE=$(recovery_count 2>/dev/null || printf '0')
  [[ $PENDING_CACHE =~ ^[0-9]+$ ]] || PENDING_CACHE=0
}

# The same number, taken from what the background collector wrote. A file that
# is missing or is not a number leaves the previous count alone: a count the
# operator can act on must not blink to zero because one collection missed it.
picker_adopt_pending() {
  local path=$1 value=""
  [[ -s $path ]] || return 0
  IFS= read -r value < "$path" 2>/dev/null || return 0
  [[ $value =~ ^[0-9]+$ ]] || return 0
  PENDING_CACHE=$value
}

# End the background collector before the picker goes away. `exec` keeps the
# process ID, so an in-flight collector would otherwise carry on writing a
# state snapshot into a file cleanup had just unlinked, and would then be
# inherited unreaped by the picker that replaced its parent.
picker_live_stop() {
  [[ -n $LIVE_PID ]] || return 0
  local child grandchild
  # The collector is a subshell whose own child is the timed status run, and
  # THAT run has its own process group and its own session (sk_timeout starts
  # one, so a terminal hangup never reaches it). Killing the direct child alone
  # left the real work running, detached, until it finished on its own.
  # sk_timeout forwards a TERM to its group; reaching the grandchildren here
  # covers a wrapper that is gone or wedged.
  for child in $(pgrep -P "$LIVE_PID" 2>/dev/null || true); do
    for grandchild in $(pgrep -P "$child" 2>/dev/null || true); do
      kill -- "-$grandchild" 2>/dev/null || kill "$grandchild" 2>/dev/null || true
    done
    kill "$child" 2>/dev/null || true
  done
  kill "$LIVE_PID" 2>/dev/null || true
  wait "$LIVE_PID" 2>/dev/null || true
  LIVE_PID=
  [[ -z $LIVE_DONE ]] || command rm -f -- "$LIVE_DONE"
  LIVE_DONE=
  [[ -z $LIVE_FILE ]] || command rm -f -- "$LIVE_FILE"
  LIVE_FILE=
  [[ -z $LIVE_PENDING ]] || command rm -f -- "$LIVE_PENDING"
  LIVE_PENDING=
}

# A menu window can sit open across release rollovers, silently running old
# code against new state, every fix ships and the standing windows never see
# it. Replace this picker with the current release at the next safe point.
#
# What the person was looking at travels with it: the grouping, the compact
# setting, the filter and the page. An upgrade they never asked for must not
# also throw away the list they had set up.
picker_self_upgrade() {
  [[ -n ${SESSION_KIT_RELEASE_DIR:-} ]] || return 0
  # Resolve both sides physically. The stable login launcher does this same
  # current -> immutable release selection for a fresh window; here the old
  # picker already knows its own immutable release from session_kit_common.
  local root=${SESSION_KIT_ROOT:-$HOME/.local/lib/session-kit}
  local current_link current running launcher release_name release_id warning_key
  current_link=$root/current
  running=$(cd -P -- "$SESSION_KIT_RELEASE_DIR" 2>/dev/null && pwd) || return 0
  if ! current=$(cd -P -- "$current_link" 2>/dev/null && pwd); then
    # Source checkouts and isolated fixtures have no activation link at all;
    # there is no attempted upgrade to diagnose in that case.
    [[ -L $current_link ]] || return 0
    # A dangling activation link cannot name an executable release. Say so
    # once, but do not disturb the picker that is still serving this window.
    warning_key="unresolved:$(readlink -- "$current_link" 2>/dev/null || printf missing)"
    if [[ ${PICKER_UPGRADE_WARNING_KEY:-} != "$warning_key" ]]; then
      PICKER_UPGRADE_WARNING_KEY=$warning_key
      LIVE_WARNING="The upgraded release is unavailable. Continuing with this picker."
      return 1
    fi
    return 0
  fi
  if [[ $current == "$running" ]]; then
    PICKER_UPGRADE_WARNING_KEY=
    return 0
  fi

  release_name=${current##*/}
  release_id=${release_name:0:12}
  launcher=$current/bin/shpool_login_launcher
  warning_key="broken:$current"
  # A target that already failed a handoff in this window is broken until
  # `current` changes again. The marker survives the recovery exec, so the
  # relaunched picker warns once instead of tearing itself down every idle
  # beat against the same corpse, the retry storm two review lanes built.
  if [[ ${SESSION_KIT_PICKER_UPGRADE_FAILED_TARGET:-} == "$current" ]]; then
    if [[ ${PICKER_UPGRADE_WARNING_KEY:-} != "$warning_key" ]]; then
      PICKER_UPGRADE_WARNING_KEY=$warning_key
      LIVE_WARNING="Release $release_id could not serve this window. Continuing with this picker."
      return 1
    fi
    return 0
  fi
  # This is the release-owned launcher selected by the stable login front
  # door. Validate what is cheaply knowable before stopping children or
  # removing any old temp file, but no static probe can prove a program will
  # SERVE (an env-shebang launcher execs fine and exits 127 a beat later), so
  # the real guarantee is the supervised handoff below, not this check.
  if [[ ! -f $launcher || -L $launcher || ! -x $launcher ]]; then
    if [[ ${PICKER_UPGRADE_WARNING_KEY:-} != "$warning_key" ]]; then
      PICKER_UPGRADE_WARNING_KEY=$warning_key
      LIVE_WARNING="Release $release_id is unavailable. Continuing with this picker."
      return 1
    fi
    return 0
  fi

  sk_log_action picker_exit release_upgrade || true
  export SESSION_KIT_PICKER_GROUP=$PICKER_GROUP_MODE
  export SESSION_KIT_PICKER_COMPACT=$PICKER_COMPACT
  export SESSION_KIT_PICKER_MACHINE_EXPANDED=$PICKER_MACHINE_EXPANDED
  export SESSION_KIT_PICKER_RESUME_QUERY=$QUERY
  export SESSION_KIT_PICKER_RESUME_PAGE=$PAGE
  cleanup
  printf 'Reloading the picker into release %s.\n' "$release_id"
  # Supervised handoff: the new picker runs as this shell's foreground child
  # and owns the terminal. If it serves, this shell is a dormant wrapper that
  # propagates the eventual exit. If it dies within the probation window
  # (exec failure, missing env command, startup crash), the window has NOT
  # been upgraded, this shell says so, marks the target failed, and relaunches
  # the release it was serving seconds ago. Announcing success is the one
  # thing that never happens before the child has outlived probation.
  local handoff_started handoff_ended handoff_status handoff_elapsed_ms
  local probation probation_ms
  probation=${SESSION_KIT_PICKER_HANDOFF_PROBATION_SECONDS:-5}
  # Bounded digits, forced base ten: bare [0-9]+ accepted "08", which bash
  # arithmetic reads as broken octal and ABORTS on, and a 16-digit value
  # multiplied by 1000 wrapped the probation negative, both demonstrated by
  # review round five, both ending the picker. Six digits is 11.5 days.
  # Zero is not a probation: "0" (and "000000") disabled the guard entirely,
  # so an instant status-0 corpse was announced as served and the picker
  # ended with no recovery (review lane rv-rn6-1). The whole point of the
  # window is catching corpses; a value that cannot catch one is invalid.
  if [[ $probation =~ ^[0-9]{1,6}$ ]] && (( 10#$probation > 0 )); then
    probation=$(( 10#$probation ))
  else
    probation=5
  fi
  probation_ms=$(( probation * 1000 ))
  # EPOCHREALTIME, not SECONDS, when it exists: integer seconds rounded a
  # 4.2-second corpse up to the probation and accepted it as served (review
  # lane rv-rn4-2). Milliseconds leave no boundary to round across. The
  # separator is the locale's, a decimal comma aborted the arithmetic in
  # round five, so strip every non-digit rather than assume the point.
  # The expansion is guarded: EPOCHREALTIME is a Bash 5.0 variable, this
  # script promises Bash 4, and under nounset a bare ${EPOCHREALTIME//...}
  # ABORTS the shell after the reload line with no recovery (review lane
  # rv-rn6-2). On a shell without it, the builtin printf's %(%s)T strftime
  # clock (Bash 4.2) stands in, a builtin reading the system clock, never a
  # shell variable. SECONDS is deliberately NOT the fallback: it is mutable
  # shell state, and a hostile BASH_ENV turned it into a line counter
  # (declare -n SECONDS=LINENO) that credited an instant corpse with fifteen
  # seconds it never lived (review lanes rv-rn7-1/2). The whole-second
  # reading is discounted by one full second below so rounding can never
  # credit a corpse with time it did not live. A shell with neither clock
  # reads zero elapsed and takes the recovery path, the direction that
  # never announces a served picker falsely.
  local handoff_clock=millisecond
  handoff_started=${EPOCHREALTIME-}
  handoff_started=${handoff_started//[!0-9]/}
  if [[ ! $handoff_started =~ ^[0-9]{1,18}$ ]]; then
    handoff_clock=second
    handoff_started=''
    builtin printf -v handoff_started '%(%s)T' -1 2>/dev/null || handoff_started=''
  fi
  "$launcher"
  handoff_status=$?
  if [[ $handoff_clock == millisecond ]]; then
    handoff_ended=${EPOCHREALTIME-}
    handoff_ended=${handoff_ended//[!0-9]/}
    if [[ $handoff_ended =~ ^[0-9]{1,18}$ ]]; then
      handoff_elapsed_ms=$(( (10#$handoff_ended - 10#$handoff_started) / 1000 ))
    else
      # A clock that cannot be read is not a served picker: zero elapsed
      # takes the recovery path, which can only ever cost one relaunch, the
      # failure direction that never accepts a corpse.
      handoff_elapsed_ms=0
    fi
  else
    handoff_ended=''
    builtin printf -v handoff_ended '%(%s)T' -1 2>/dev/null || handoff_ended=''
    if [[ $handoff_started =~ ^[0-9]{1,18}$ && $handoff_ended =~ ^[0-9]{1,18}$ ]] \
      && (( 10#$handoff_ended > 10#$handoff_started )); then
      # One whole second is surrendered before comparing: a corpse dying at
      # eighty percent of the probation must not be rounded across the line
      # it never reached (rv-rn4-2's credit, kept out of the fallback too).
      handoff_elapsed_ms=$(( (10#$handoff_ended - 10#$handoff_started - 1) * 1000 ))
    else
      handoff_elapsed_ms=0
    fi
  fi
  # Two verdicts mean the child never served this window, whatever its exit
  # status says. An instant zero exit is a corpse wearing a success code, a
  # person cannot open and leave a picker faster than it draws (review lane
  # rv-rn3-2's zero-exit corpse). And death by signal is never a served
  # picker's own ending: a real picker handles INT by redrawing, so 128+N
  # means a hang someone had to kill (the same lane's non-serving hang).
  if (( handoff_elapsed_ms >= probation_ms && handoff_status < 128 )); then
    # It served. Its ending, a person leaving, or its own exit, passes
    # through untouched.
    exit "$handoff_status"
  fi
  if (( handoff_status == 0 )); then
    printf 'The release %s picker ended before it could serve. Reloading this one.\n' \
      "$release_id"
  else
    printf 'The release %s picker exited immediately (status %s). Reloading this one.\n' \
      "$release_id" "$handoff_status"
  fi
  sk_log_action picker_exit release_upgrade_failed || true
  export SESSION_KIT_PICKER_UPGRADE_FAILED_TARGET=$current
  shopt -s execfail
  exec "$SESSION_KIT_RELEASE_DIR/bin/shpool_login_launcher"
  printf 'No picker could start in this window. Run shpool_login by hand.\n'
  exit 1
}
