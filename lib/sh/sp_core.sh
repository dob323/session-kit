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

attach_id() {
  local id=$1 cwd=${2:-} force=${3:-0}
  local -a args=(attach)
  [[ $force == 1 ]] && args+=(--force)
  args+=(--cmd /bin/false)
  [[ -z $cwd ]] || args+=(--dir "$cwd")
  args+=("$id")
  sk_push_session_color "${SK_PROVIDER:-}" "${SK_UUID:-}"
  assert_input_modes
  exec "$SK_SHPOOL" "${args[@]}"
}

resolve_target() {
  local selector=$1
  make_snapshot || return 1
  sk_resolve "$SNAPSHOT" "$selector" || {
    sk_die "no unique session matches that selection"
    return 1
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
