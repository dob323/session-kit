#!/usr/bin/env bash
# sp snapshot construction, attach primitives, and target resolution: the
# building blocks every sp action starts from. Source this file; do not execute
# it.
#
# Source order: bin/sp sources this module after it resolves SCRIPT_DIR,
# sources bin/session_kit_common, and assigns INVENTORY_CORE, and before it
# installs its first trap, so trap cleanup_snapshot EXIT always names a defined
# function. SNAPSHOT is assigned by the make_*_snapshot functions here and read
# across every other sp module.
#
# Globals the entry script owns are assigned there, not here.
# shellcheck disable=SC2154

make_snapshot() {
  sk_prepare_state || return 1
  [[ -z ${SNAPSHOT:-} || ! -f $SNAPSHOT ]] || command rm -- "$SNAPSHOT"
  SNAPSHOT=$(mktemp "$SK_STATE_DIR/sp-snapshot.json.XXXXXX") || return 1
  sk_snapshot_file "$SNAPSHOT"
}

make_strict_snapshot() {
  sk_prepare_state || return 1
  [[ -z ${SNAPSHOT:-} || ! -f $SNAPSHOT ]] || command rm -- "$SNAPSHOT"
  SNAPSHOT=$(mktemp "$SK_STATE_DIR/sp-strict.json.XXXXXX") || return 1
  sk_strict_snapshot_file "$SNAPSHOT"
}

make_guard_snapshot() {
  sk_prepare_state || return 1
  [[ -z ${SNAPSHOT:-} || ! -f $SNAPSHOT ]] || command rm -- "$SNAPSHOT"
  SNAPSHOT=$(mktemp "$SK_STATE_DIR/sp-guard.json.XXXXXX") || return 1
  sk_guard_snapshot_file "$SNAPSHOT"
}

cleanup_snapshot() {
  if [[ -n ${SNAPSHOT:-} && -f $SNAPSHOT ]]; then
    command rm -- "$SNAPSHOT"
  fi
}

# Fire-and-forget provider color push at attach time. The transcript is
# guaranteed to exist by now, unlike at creation proof, and the attach itself
# is never delayed. Codex is a clean no-op inside the propagator. The kit
# never alters the attached terminal itself; the provider applies its own
# native color from this record at the session's next start or resume.
sk_push_session_color() {
  [[ -n ${1:-} && -n ${2:-} ]] || return 0
  ( python3 "$INVENTORY_CORE" color propagate "$1" "$2" >/dev/null 2>&1 \
      || true ) &
}

# Give the attach client a terminal on stderr when the front door took it
# away, or the client will refuse to take the terminal at all -- silently.
#
# libshpool/src/tty.rs:91 (`set_attach_flags`): if ANY of stdin, stdout or
# stderr is not a tty, shpool returns a no-op guard and never sets raw mode,
# then pipes bytes anyway. The picker has already restored cooked mode by then
# (lib/sh/shpool_login_live.sh:156, before every action), so the person is left
# typing into a COOKED terminal with a live, pumping client: every keystroke
# echoes as literal text over the application's first screen, Ctrl-L and Esc do
# nothing because ICANON holds them until a newline, and nothing reaches the
# session.
#
# THIS IS THE SECOND LINE OF DEFENCE, NOT THE ROOT CAUSE.
#
#   The root cause of a deaf terminal is a separate one-line bug, fixed on
#   its own branch: `picker_events_stop` at
#   lib/sh/shpool_login_live.sh:376 runs
#
#       exec {PICKER_EVENTS_FD}<&- 2>/dev/null
#
#   and in bash a redirection on a BARE `exec` sticks to the shell for good --
#   so closing the event descriptor also pointed the picker's own stderr at
#   /dev/null permanently. The order that produces it: a picker open long
#   enough for its event stream to drop, the stop path poisoning fd 2, a
#   self-upgrade carrying /dev/null through its `exec` into the new picker,
#   and the next client attaching with a non-terminal fd 2 and never
#   touching the terminal. From there the session is deaf and says nothing
#   about it, for as long as it takes somebody to notice.
#
#   Fixing that one line closes that one source. This function closes the
#   CLASS: whatever hands a non-terminal descriptor down -- a stale /dev/null
#   from a bare `exec`, the TUI launcher's stderr FIFO
#   (bin/shpool_login_launcher:75), or the next one nobody has found -- the
#   client is given a terminal on all three before the handover, so it can
#   never silently decline again. Two doors, one class of bug.
#
#   Measured, not assumed. Real bin/shpool_login, real `sp`, real client, real
#   pty, with the picker poisoned by its OWN code (a dropped event stream, so
#   line 376 runs for real):
#
#       base 68fd53e   19,532 of 19,534 samples cooked with a live client,
#                      101.7 s, and the client never wrote termios at all
#       with this      0 of 19,176 cooked, and the client takes the terminal
#
#   The TUI door is measured too, through the real launcher and its real FIFO:
#   986 of 986 samples cooked at base, 0 of 927 here.
#
# Deliberately narrow. The first version opened on "a terminal on any one of
# the three" and rebound the other two, which took redirections away from
# callers that had asked for them -- `sp go 3 > out` wrote to the screen and
# left `out` empty. The rules now:
#
#   * stderr is already a terminal -> nothing to do, nothing measured, no cost.
#   * stdin or stdout is NOT a terminal -> the caller redirected a channel it
#     is reading or writing. Honour it and stay out of the way.
#   * otherwise point stderr at the terminal STDOUT already has -- not at
#     /dev/tty. The controlling terminal is not always the terminal on fd 0/1
#     (nested ptys, which are ordinary over SSH), and binding to /dev/tty
#     could put this session's errors on a different screen.
sk_bind_handoff_tty() {
  [[ -t 2 ]] && return 0
  [[ -t 0 && -t 1 ]] || return 0
  exec 2>&1
}

# The same binding, but scoped to one attach, plus the repair net below.
#
# `attach_id` ends in `exec`, so its binding dies with the process. The picker
# door does NOT exec -- it runs the client as a child -- so an unscoped
# `exec 2>` outlived the attach and everything `sp` said afterwards bypassed
# the launcher's FIFO capture and arrived out of order on the screen. Arm
# before the client, release after it.
SK_HANDOFF_SESSION=
SK_HANDOFF_MODES=
SK_HANDOFF_ERR=

sk_handoff_guard_arm() {
  SK_HANDOFF_SESSION=${1:-}
  SK_HANDOFF_MODES=
  SK_HANDOFF_ERR=
  # What the terminal looked like before the session took it. This is what
  # gets put back if the session leaves it unusable, and it is exact -- the
  # person's own settings, not a guessed `stty sane`.
  if [[ -t 0 ]]; then
    SK_HANDOFF_MODES=$(command stty -g <&0 2>/dev/null || true)
  fi
  if [[ ! -t 2 && -t 0 && -t 1 ]]; then
    exec {SK_HANDOFF_ERR}>&2
    exec 2>&1
  fi
}

# Put stderr back where the caller had it. Called on every exit path,
# including the traps, so an interrupted `sp` cannot leave the FIFO bypassed.
sk_handoff_unbind() {
  if [[ -n ${SK_HANDOFF_ERR:-} ]]; then
    exec 2>&"$SK_HANDOFF_ERR"
    exec {SK_HANDOFF_ERR}>&-
    SK_HANDOFF_ERR=
  fi
  return 0
}

# The net: after the session has handed the terminal back, check that a person
# can actually use it, and put it right if they cannot.
#
# This is the `stty raw -echo` correction the operator had to run from another
# window on 2026-08-15, done from inside the handoff instead. It runs ONLY
# after the client has exited -- never while a client owns the terminal, so it
# can never fight one -- and it does nothing at all, and says nothing, when the
# terminal comes back the way it went in. That is the common case and it costs
# one `stty -g`.
#
# When it does fire it SHOUTS, because a net that catches silently destroys the
# evidence for whoever is still hunting the cause.
sk_repair_handoff_terminal() {
  local client_status=${1:-0}
  [[ -n ${SK_HANDOFF_MODES:-} && -t 0 ]] || return 0
  local now
  now=$(command stty -g <&0 2>/dev/null) || return 0
  [[ $now != "$SK_HANDOFF_MODES" ]] || return 0
  # `stty -g` is iflag:oflag:cflag:lflag:cc... in hex on Linux. Field four is
  # the local flags, where ECHO is 0x8 and ICANON is 0x2 -- the two a person
  # needs to type at a shell prompt. A format this cannot read (macOS prints
  # `gfmt1:...`) is left alone rather than guessed at.
  local rest=${now#*:} lflag
  rest=${rest#*:}
  rest=${rest#*:}
  lflag=${rest%%:*}
  [[ $lflag =~ ^[0-9a-fA-F]+$ ]] || return 0
  local echo_on=0 icanon_on=0
  (( 0x$lflag & 0x8 )) && echo_on=1
  (( 0x$lflag & 0x2 )) && icanon_on=1
  if (( echo_on && icanon_on )); then
    return 0
  fi
  command stty "$SK_HANDOFF_MODES" <&0 2>/dev/null || return 0
  sk_shout_handoff_repair "$lflag" "$echo_on" "$icanon_on" "$client_status"
  return 0
}

# One line on the screen so the person knows what happened, one record with the
# measurements so the next investigator has something to work from.
sk_shout_handoff_repair() {
  local lflag=$1 echo_on=$2 icanon_on=$3 client_status=$4
  local tty_name pgrp members stamp
  tty_name=$(command tty <&0 2>/dev/null || true)
  pgrp=$(command ps -o tpgid= -p $$ 2>/dev/null | command tr -cd '0-9' || true)
  members=$(command ps -e -o pgid=,pid=,comm= 2>/dev/null \
    | command awk -v want="${pgrp:-none}" \
        '$1 == want { printf "%s:%s ", $2, $3 }' || true)
  stamp=$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || true)
  printf 'session-kit: that session left this terminal unusable (no echo or no line mode); your input modes have been restored. Recorded in %s\n' \
    "$SK_STATE_DIR/handoff-repair.log" >&2
  printf '%s\tsession=%s\ttty=%s\tlflag=0x%s\tECHO=%s\tICANON=%s\tfg_pgrp=%s\tmembers=%s\tclient_status=%s\trestored=%s\n' \
    "${stamp:-unknown}" "${SK_HANDOFF_SESSION:-unknown}" \
    "${tty_name:-unknown}" "$lflag" "$echo_on" "$icanon_on" \
    "${pgrp:-unknown}" "${members:-none}" "$client_status" \
    "$SK_HANDOFF_MODES" \
    >> "$SK_STATE_DIR/handoff-repair.log" 2>/dev/null || true
  sk_log_action handoff_repair fired || true
  return 0
}

# Release everything the guard holds. Two entry points, on purpose:
#   `sk_handoff_guard_release` -- the client has exited and was waited on, so
#       the terminal is ours to inspect and repair.
#   `sk_handoff_guard_abandon` -- a trap fired. A client may still be running,
#       so stderr goes back and NOTHING touches the terminal: the one rule this
#       net must never break is fighting a live client for the line discipline.
sk_handoff_guard_release() {
  local client_status=${1:-0}
  sk_handoff_unbind
  sk_repair_handoff_terminal "$client_status"
  SK_HANDOFF_SESSION=
  SK_HANDOFF_MODES=
  return 0
}

sk_handoff_guard_abandon() {
  sk_handoff_unbind
  SK_HANDOFF_SESSION=
  SK_HANDOFF_MODES=
  return 0
}

assert_input_modes() {
  # shpool's restore replays screen contents only, never the input modes the
  # application enabled, so a freshly opened window loses bracketed paste on
  # reattach: terminals flag multi-line pastes as unsafe and every newline
  # submits early. Re-arm bracketed paste before handing the terminal to
  # shpool; the restore buffer carries no mode resets, so this survives the
  # replay. The optional shpool-patch/0002 makes the daemon replay the full
  # tracked mode state itself.
  if [[ -t 1 ]]; then
    printf '\033[?2004h'
  fi
}

# Refill the terminal's scrollback before shpool takes the screen (operator
# finding 2026-08-14). Restore mode "simple" repaints only the live frame, so
# a freshly opened window starts with no history at all -- the operator could
# scroll back "only a couple screens". Replay the tail of the session's
# SETTLED rendered journal: the same clean text `sp history` pages, never raw
# bytes (raw TUI repaints braid -- operator finding 2026-08-12). Settled-only
# also keeps the joint clean: the application repaints its own live frame
# after attach, so replaying the unsettled screen here would show every frame
# twice. Best-effort by design -- no journal, no renderer, a failed or slow
# render, or a non-terminal stdout all skip the refill rather than delay or
# block the attach; a sidecar the renderer could not freshen is replayed
# stale, which for scrollback still beats nothing.
sk_replay_history() {
  local id=$1 lines=${SESSION_KIT_ATTACH_HISTORY:-500}
  [[ -t 1 && -n $id ]] || return 0
  case $lines in '' | *[!0-9]*) lines=500 ;; esac
  # All digits from here, but not yet a safe number: a leading zero sends
  # bash arithmetic into octal ("08" printed an error onto the operator's
  # terminal) and a value past 9 digits can wrap 64-bit arithmetic negative.
  # Both must end as a sane bound, never an error (lane finding F3).
  if (( ${#lines} > 9 )); then lines=999999999; fi
  lines=$((10#$lines))
  (( lines > 0 )) || return 0
  local -a files=()
  local path
  while IFS= read -r path; do [[ -n $path ]] && files+=("$path"); done \
    < <(history_files "$id")
  (( ${#files[@]} > 0 )) || return 0
  local journal sidecar state render_tool
  if [[ -d $SK_JOURNAL_DIR/$id ]]; then
    journal=$SK_JOURNAL_DIR/$id
    sidecar=$journal/rendered.txt
    state=$journal/rendered.state.json
  else
    journal=${files[0]}
    sidecar=${journal%.raw}.rendered.txt
    state=${journal%.raw}.rendered.state.json
  fi
  if sk_test_hook SESSION_KIT_JOURNAL_RENDER_TOOL; then
    render_tool=$SK_TEST_HOOK
  else
    render_tool=${SK_INVENTORY_CORE%/*}/sessionkit_inventory/journal_render.py
  fi
  if [[ -f $render_tool ]]; then
    # One renderer per sidecar at a time: journal_render.py takes no lock of
    # its own, and two concurrent renders append the same settled lines twice
    # -- permanently, because the state file commits the inflated length
    # (lane finding O1). Contended means someone else is freshening this
    # sidecar right now; skip and replay whatever it already holds. The
    # timeout wrapper kills the whole process group, so the renderer never
    # outlives the cap holding the lock. util-linux flock(1) is deliberately
    # absent on macOS (sk_lock_acquire branches the same way): without it,
    # render unlocked -- the pre-lock behaviour -- rather than losing the
    # refill to a missing locker (lane finding F4).
    local -a render_locker=()
    if command -v flock >/dev/null 2>&1; then
      render_locker=(flock -n "$sidecar.lock")
    fi
    sk_timeout 4 ${render_locker[@]+"${render_locker[@]}"} \
      python3 "$render_tool" render \
      --journal "$journal" --out "$sidecar" --state "$state" \
      >/dev/null 2>&1 || true
  fi
  [[ -r $sidecar && -s $sidecar ]] || return 0
  command tail -n "$lines" -- "$sidecar" 2>/dev/null || return 0
  printf '\n%s\n' \
    '── earlier output above (rendered) · complete record: sp history ──'
  # Push the replay fully into scrollback before the application repaints.
  # A TUI redraws its frame relative to the current cursor, so without this
  # pad it paints OVER the tail of the replayed block and the visible screen
  # becomes interleaved mush (operator finding X19-b, 2026-08-14: a
  # transferred session showed "over written garbage"). A screenful of blank
  # rows gives the repaint a clean region; the replay stays intact above it.
  # Ask the terminal itself, through a dup of stdout taken OUTSIDE the
  # command substitution. Inside $( ) stdout is a pipe, so tput cannot ioctl
  # the terminal and silently answers the terminfo constant 24 on every
  # window (lane finding X19-c: the pad was 24 rows on a 120-row window,
  # leaving 96 rows of replay for the TUI to paint over). stty reads the fd
  # it is given, needs no terminfo, and stays silent on failure.
  local pad_rows sk_pad_tty
  exec {sk_pad_tty}>&1
  pad_rows=$(command stty size <&"$sk_pad_tty" 2>/dev/null) || pad_rows=""
  exec {sk_pad_tty}>&-
  pad_rows=${pad_rows%% *}
  case $pad_rows in '' | *[!0-9]*) pad_rows=50 ;; esac
  (( ${#pad_rows} > 4 )) && pad_rows=200
  pad_rows=$((10#$pad_rows))
  # A zero or tiny reported height (an unsized pty) still needs a real pad.
  (( pad_rows >= 5 )) || pad_rows=50
  (( pad_rows > 200 )) && pad_rows=200
  local padding=""
  local -i pad_index
  for (( pad_index = 0; pad_index < pad_rows; pad_index++ )); do
    padding+=$'\n'
  done
  printf '%s' "$padding"
}

attach_id() {
  local id=$1 cwd=${2:-} force=${3:-0}
  local -a args=(attach)
  [[ $force == 1 ]] && args+=(--force)
  args+=(--cmd /bin/false)
  [[ -z $cwd ]] || args+=(--dir "$cwd")
  args+=("$id")
  # First, before anything that tests -t 1: the refill and the bracketed-paste
  # re-arm below are both skipped on a non-terminal stdout, and the client
  # itself refuses raw mode on a non-terminal ANY of the three.
  sk_bind_handoff_tty
  sk_push_session_color "${SK_PROVIDER:-}" "${SK_UUID:-}"
  # `sp go` / `sp attach` / `sp takeover` typed at a shell end here; the
  # picker's own open and takeover end at picker_attach_id in sp_picker.sh.
  # Both doors write the kit's tab name and both refill scrollback -- a
  # change to one belongs in the other (X19 lane finding F1 was exactly
  # this pair drifting apart).
  sk_tab_title "$(sk_human_label "${SK_TITLE:-}" "${SK_PROVIDER:-}" "${SK_NUMBER:-}")"
  sk_replay_history "$id"
  assert_input_modes
  # `exec` replaces this process, so `trap cleanup_snapshot EXIT` never fires:
  # every `sp go` and every `sp takeover` left a ~30 KB snapshot behind, forever
  # -- the only leak class with no upper bound. Dropping it here covers all
  # three callers rather than each remembering separately.
  cleanup_snapshot
  if [[ $force == 1 ]] &&
     ! sk_shpool_holder_is "$SK_DAEMON_PID" "$SK_DAEMON_START"; then
    sk_die "the socket author changed before takeover; nothing was moved"
    return 1
  fi
  exec "$SK_SHPOOL" "${args[@]}"
}

# One selector, one refusal, one status. Every verb that takes a <session>
# lands here, so a wrong number reads the same whichever verb typed it, and
# `sp help exit-codes` can say 2 and mean it.
resolve_target() {
  local selector=$1
  make_snapshot || return 1
  sk_resolve "$SNAPSHOT" "$selector" || {
    sk_die "no session matches that selector"
    return 2
  }
}

history_files() {
  local id=$1 recovered
  recovered=$(sk_recovery_journal "$id" 2>/dev/null || true)
  if [[ -n $recovered && -r $recovered ]]; then
    printf '%s\n' "$recovered"
  elif [[ -r $SK_JOURNAL_DIR/$id.raw ]]; then
    printf '%s\n' "$SK_JOURNAL_DIR/$id.raw"
  elif [[ -d $SK_JOURNAL_DIR/$id ]]; then
    find "$SK_JOURNAL_DIR/$id" -maxdepth 1 -type f -name 'segment-*.raw' -print | sort
  fi
}
