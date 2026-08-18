#!/usr/bin/env bash
# Picker palette and terminal surface. Source this file; do not execute it.
#
# Source order: bin/shpool_login sources this module after it resolves
# SCRIPT_DIR, sources bin/session_kit_common, and assigns PICKER_SCREEN, PICKER_STYLE,
# PICKER_BOLD, PICKER_GREEN, and PICKER_RESET, and before it installs its first
# trap.
#
# Globals the entry script owns are assigned there, not here.
# shellcheck disable=SC2154

picker_bold() {
  if (( PICKER_STYLE )); then
    printf '%s%s%s' "$PICKER_BOLD" "$1" "$PICKER_RESET"
  else
    printf '%s' "$1"
  fi
}

picker_green() {
  if (( PICKER_STYLE )); then
    printf '%s%s%s%s' "$PICKER_BOLD" "$PICKER_GREEN" "$1" "$PICKER_RESET"
  else
    printf '%s' "$1"
  fi
}

# The attachment that just ended painted its own frames; without a clear the
# menu overprints the dead session's screen and reads as corruption (seen
# live at the 2026-08-02 daemon restart). Screen control is independent of
# color: NO_COLOR removes styling, not safe cursor and clear-screen control.
picker_clear_screen() {
  (( PICKER_SCREEN )) && printf '\033[2J\033[H'
  return 0
}

# Compute-first frames: a renderer runs with stdout captured (in the current
# shell, so its state side effects survive), and the finished bytes reach the
# terminal as ONE write. In `home` mode each replaced line erases its own
# remainder and the frame ends by erasing below, nothing is blanked until
# its replacement is in the same write, so a slow renderer can never expose
# an empty screen, with or without DEC-2026 support. `append` mode emits the
# captured block in one write at the cursor, which turns a progressively
# painted menu into a single paint.
picker_frame_emit_file() {
  local mode=$2 content
  content=$(cat -- "$1" && printf x) || return 0
  content=${content%x}
  if (( PICKER_SCREEN )) && [[ $mode == home ]]; then
    content=${content//$'\n'/$'\033[K\n'}
    printf '\033[?2026h\033[H%s\033[K\033[J\033[?2026l' "$content"
  else
    # Append mode: the capture already makes this one write; extra control
    # sequences would change the screen contract for no gain.
    printf '%s' "$content"
  fi
  return 0
}

terminal_height() {
  local height=${LINES:-}
  if [[ ! $height =~ ^[0-9]+$ ]]; then
    height=$(tput lines 2>/dev/null || true)
  fi
  [[ $height =~ ^[0-9]+$ ]] || height=24
  # Respect genuinely short windows down to 8 rows: budgeting for 12 lines an
  # 8-row split cannot show scrolled the summary and first sessions off-screen
  # before the human could read them.
  (( height >= 8 )) || height=8
  (( height <= 200 )) || height=200
  printf '%s\n' "$height"
}
