#!/usr/bin/env bash
# Picker palette and terminal surface. Source this file; do not execute it.
#
# Source order: bin/shpool_login sources this module after it resolves
# SCRIPT_DIR, sources bin/session_kit_common, and assigns PICKER_STYLE,
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
# live at the 2026-08-02 daemon restart). Gated on the same test as color:
# a dumb or capability-less terminal must never receive the escapes.
picker_clear_screen() {
  (( PICKER_STYLE )) && printf '\033[2J\033[H'
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
