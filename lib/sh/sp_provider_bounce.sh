#!/usr/bin/env bash
# The one safe provider bounce, per provider: terminate an exact idle TUI that
# booted before its title existed so the session shell relaunches the same
# conversation named and themed. Every guard fails open toward no bounce.
# Source this file; do not execute it.
#
# Source order: bin/sp sources this module ahead of its first trap, after
# SCRIPT_DIR, session_kit_common, and INVENTORY_CORE are in place. These
# functions read the SK_PROOF_* variables that picker_load_fresh() in
# lib/sh/sp_picker.sh exports before it calls them.
#
# Globals the entry script owns are assigned there, not here.
# shellcheck disable=SC2154

# The bounce marker, written whole or not written at all.
#
# The file has three readable states and each one means something different to
# a different reader: absent is "no bounce", naming a conversation is "bounce
# this one, nobody has yet", and EMPTY is the session shell's receipt that it
# has taken the instruction and is relaunching. Collection removes a marker
# only in that last state, because emptiness is the only one that proves the
# bounce already happened.
#
# A plain `> file` redirect truncates before it writes, so it passes through
# the empty state on its way to the full one. A collection landing in that
# split second reads the receipt for a bounce nobody has performed, removes
# the marker, and the shell later finds no instruction where it was told to
# look -- it takes the ordinary provider exit and the session closes. A rename
# has no such moment: the marker appears already complete.
sk_write_bounce_marker() {
  local id=$1 uuid=$2
  local directory="$SK_STATE_DIR/provider-bounce"
  local temporary reservation generation
  ( umask 077; mkdir -p "$directory" ) 2>/dev/null || return 1
  # Repairs a directory an older release created before the umask was right;
  # a marker readable by anything else is state somebody else can forge.
  chmod 700 "$directory" 2>/dev/null || true
  # Created 0600 by mktemp, and dot-prefixed so it is never mistaken for a
  # session ID; the marker sweep removes a stray one after the same grace it
  # gives every other file here.
  # The reservation remains for this session's lifetime. Therefore mktemp can
  # never hand a later request the same receipt generation while any stale
  # collector for this session can still be in flight.
  reservation=$(mktemp \
    "$directory/.generation.$id.taken.XXXXXXXXXXXX" 2>/dev/null) || return 1
  generation=${reservation##*.}
  temporary=$(mktemp "$directory/.$id.XXXXXXXXXXXX" 2>/dev/null) || {
    command rm -f -- "$reservation" 2>/dev/null
    return 1
  }
  # The third line names this request generation. The non-hex padding keeps
  # that generation beyond the first 64 bytes, so provider shells already
  # running the previous release still parse the UUID exactly and fall back to
  # their safe legacy receipt name.
  if ! printf '%s\nxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n%s\n' \
       "$uuid" "$generation" > "$temporary" 2>/dev/null ||
     ! mv -f -- "$temporary" "$directory/$id" 2>/dev/null; then
    command rm -f -- "$temporary" "$reservation" 2>/dev/null
    return 1
  fi
  return 0
}

# One safe provider bounce: a new-mode Codex booted before its thread title
# existed, so its status bar shows the conversation ID forever. When the human
# opens such a session and a title now exists, terminate the exact idle TUI;
# the session shell relaunches the SAME conversation immediately (marker
# handshake with the bashrc), so the window opens named and themed. Guards,
# all fail-open toward "no bounce": untitled-boot marker present (only
# new-mode Codex sessions ever write one, so parked or pre-existing lanes are
# structurally excluded), a title in Codex's thread store, no subagents, not
# mid-turn, quiet for two minutes, and an exact PID+start-time match before
# the signal. App-server sessions keep the server and socket alive and restart
# only their one generation-bound remote TUI.
picker_bounce_codex() {
  local id=$1 mode=${2:-automatic} reason=${3:-name}
  [[ $mode == automatic || $mode == explicit ]] || return 2
  [[ $reason == name || $reason == color ]] || return 2
  [[ ${SK_PROOF_PROVIDER:-} == codex ]] || return 1
  local uuid=${SK_PROOF_UUID:-}
  [[ -n $uuid ]] || return 1
  local untitled="$SK_STATE_DIR/provider-untitled/$id"
  [[ -f $untitled && ! -L $untitled ]] || return 1
  # Active markers never age out. The inventory reaper quarantines a marker
  # only after a fresh shpool snapshot proves that exact session is absent.
  local pid=${SK_PROOF_PROVIDER_PID:-} start=${SK_PROOF_PROVIDER_START:-}
  [[ $pid =~ ^[0-9]+$ && $start =~ ^[0-9]+$ ]] || return 1
  local refresh_target refresh_pid refresh_start
  refresh_target=$(python3 "$INVENTORY_CORE" platform codex-refresh-target \
    "${SK_PROOF_SHELL_PID:-}" "${SK_PROOF_SHELL_START:-}" \
    "$pid" "$start" 2>/dev/null) || return 1
  IFS=$'\t' read -r refresh_pid refresh_start <<<"$refresh_target"
  [[ $refresh_pid =~ ^[0-9]+$ && $refresh_start =~ ^[0-9]+$ ]] || return 1
  python3 - "$SNAPSHOT" "$id" "$uuid" "$pid" "$start" "$mode" <<'PY' || return 1
import json, sys

snapshot, shpool_id, uuid, pid_text, start_text, mode = sys.argv[1:7]
try:
    data = json.load(open(snapshot, encoding="utf-8"))
    row = next(
        s for s in data.get("sessions", [])
        if s.get("shpool_id") == shpool_id
    )
except (OSError, ValueError, StopIteration):
    raise SystemExit(1)
if mode == "automatic" and row.get("availability") != "ready":
    raise SystemExit(1)
identity = row.get("identity") or {}
if (
    identity.get("uuid") != uuid
    or str(identity.get("pid")) != pid_text
    or str(identity.get("process_start_ticks")) != start_text
):
    raise SystemExit(1)
if row.get("subagents"):
    raise SystemExit(1)
# Positive proof of a stable state, never "not working": "working" is
# mid-computation and "state unavailable" proves nothing, so both refuse.
# "needs your reply"/"reply optional" are TERM-safe wait states, the
# turn's computation is complete and only a human reply advances it. A
# research-style session spends its whole life in "needs your reply", so
# an idle-only rule left those bars unnamed forever; the question text
# survives the resume in the transcript (only the interactive widget is
# lost, and the thread accepts the answer as a plain message). Automatic
# mode still requires availability "ready", so an attached window is
# never restarted under the human.
if str(row.get("agent_status") or "").casefold() not in (
    "idle",
    "needs your reply",
    "reply optional",
):
    raise SystemExit(1)
# With journals disabled the output age is unknown for every session; the
# rollout-derived turn state above is then the idleness signal. A known
# recent age still blocks a bounce during active use.
age = row.get("recent_output_age_seconds")
if isinstance(age, int) and not isinstance(age, bool) and age < 120:
    raise SystemExit(1)
raise SystemExit(0)
PY
  python3 "$INVENTORY_CORE" platform process-is \
    "$refresh_pid" "$refresh_start" codex \
    >/dev/null 2>&1 || return 1
  # Only a real name is worth a restart FOR A NAME: Codex seeds threads.title
  # with the raw first prompt, and bouncing on that echo burns the one-shot
  # restart before the true title exists. A refusal here leaves the untitled
  # marker in place so a later reopen retries once the thread is really named.
  #
  # A colour change is a different question with a different answer. The
  # window's theme is chosen at launch from the conversation's stored colour,
  # so a recolour needs a restart whether or not anybody has named the thread
  # -- and requiring a title here left every unnamed session stuck showing the
  # colour it picked for itself. The colour must exist, or the restart would
  # change nothing; the name is irrelevant to it.
  local bounce_title=""
  if [[ $reason == color ]]; then
    # The colour has to be one a restart can actually paint: Codex reads its
    # theme from the command line the launcher builds, so the kit theme file
    # for this colour must exist. `color effective` alone proves nothing --
    # it falls back to a hash of the identity and succeeds for any uuid.
    python3 "$INVENTORY_CORE" color bounce-ready codex "$uuid" >/dev/null 2>&1 || return 1
    # Still mirror a real name into the session index when one exists, since
    # the relaunched status bar reads only that; its absence never blocks the
    # recolour, and whether one existed decides the marker below.
    bounce_title=$(python3 "$INVENTORY_CORE" codex-bounce-title "$uuid" 2>/dev/null) ||
      bounce_title=""
  else
    bounce_title=$(python3 "$INVENTORY_CORE" codex-bounce-title "$uuid" 2>/dev/null) || return 1
    [[ -n $bounce_title ]] || return 1
  fi
  sk_write_bounce_marker "$id" "$uuid" || return 1
  # The decision above ran on a snapshot taken before the create lock was
  # released; re-prove idleness on FRESH state immediately before the TERM,
  # or a turn that started meanwhile gets killed mid-work.
  local recheck
  recheck=$(mktemp "$SK_STATE_DIR/bounce-recheck.json.XXXXXX") || {
    command rm -f -- "$SK_STATE_DIR/provider-bounce/$id"
    return 1
  }
  if ! sk_guard_snapshot_file "$recheck" 2>/dev/null ||
     ! python3 - "$recheck" "$id" "$uuid" "$pid" "$start" "$mode" <<'PY'
import json, sys

try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
    row = next(
        s for s in data.get("sessions", [])
        if s.get("shpool_id") == sys.argv[2]
    )
except (OSError, ValueError, StopIteration):
    raise SystemExit(1)
uuid, pid_text, start_text, mode = sys.argv[3:7]
identity = row.get("identity") or {}
if (
    identity.get("uuid") != uuid
    or str(identity.get("pid")) != pid_text
    or str(identity.get("process_start_ticks")) != start_text
):
    raise SystemExit(1)
if mode == "automatic" and row.get("availability") != "ready":
    raise SystemExit(1)
if row.get("subagents"):
    raise SystemExit(1)
# Same stable-state set as the pre-lock decision: refuse only
# mid-computation ("working") and unproven state.
if str(row.get("agent_status") or "").casefold() not in (
    "idle",
    "needs your reply",
    "reply optional",
):
    raise SystemExit(1)
# The same quiet test as the first pass, on fresh state. This recheck exists
# to catch work that STARTED during the race window, and output arriving in
# the last two minutes is exactly that evidence -- a status that still reads
# idle while the window is printing is the case this refuses.
age = row.get("recent_output_age_seconds")
if isinstance(age, int) and not isinstance(age, bool) and age < 120:
    raise SystemExit(1)
raise SystemExit(0)
PY
  then
    command rm -f -- "$recheck" "$SK_STATE_DIR/provider-bounce/$id"
    return 1
  fi
  if ! python3 "$INVENTORY_CORE" platform process-is \
    "$refresh_pid" "$refresh_start" codex \
    >/dev/null 2>&1; then
    command rm -f -- "$recheck" "$SK_STATE_DIR/provider-bounce/$id"
    return 1
  fi
  command rm -f -- "$recheck"
  if kill -TERM "$refresh_pid" 2>/dev/null; then
    # The marker is a one-shot: it is written when a window boots before its
    # name exists, and every later remedy -- the automatic retry at reopen and
    # the picker's "apply the pending title" action -- requires it. Spending it
    # on a restart that applied no name would strand an unnamed session's
    # status bar on its conversation ID for the rest of that window's life,
    # with both remedies gone. Only a restart that carried a name clears it.
    if [[ -n $bounce_title ]]; then
      command rm -f -- "$untitled"
    fi
    # Whatever the reason, the window has restarted, so it has read the
    # colour the kit holds. A pending recolour is no longer pending.
    command rm -f -- "$SK_STATE_DIR/provider-recolor/$id"
    picker_action_event title_refresh provider_restarted
  else
    command rm -f -- "$SK_STATE_DIR/provider-bounce/$id"
    return 1
  fi
  return 0
}

# The Claude variant of the one safe provider bounce: a Claude window
# renames only through its SessionStart/prompt hooks, so a session named
# AFTER boot with no prompt since (ask, read, close) shows a stale title
# until the provider restarts. Guards mirror the Codex bounce; the
# claude-bounce-title verb additionally proves no prompt followed the name
# intent (exit 3 = the window already renamed live, so the marker is
# dropped without any restart).
picker_bounce_claude() {
  local id=$1 mode=${2:-automatic} reason=${3:-name}
  [[ $mode == automatic || $mode == explicit ]] || return 2
  [[ $reason == name || $reason == color ]] || return 2
  [[ ${SK_PROOF_PROVIDER:-} == claude ]] || return 1
  local uuid=${SK_PROOF_UUID:-}
  [[ -n $uuid ]] || return 1
  local untitled="$SK_STATE_DIR/provider-untitled/$id"
  [[ -f $untitled && ! -L $untitled ]] || return 1
  local pid=${SK_PROOF_PROVIDER_PID:-} start=${SK_PROOF_PROVIDER_START:-}
  [[ $pid =~ ^[0-9]+$ && $start =~ ^[0-9]+$ ]] || return 1
  python3 - "$SNAPSHOT" "$id" "$uuid" "$pid" "$start" "$mode" <<'PY' || return 1
import json, sys

snapshot, shpool_id, uuid, pid_text, start_text, mode = sys.argv[1:7]
try:
    data = json.load(open(snapshot, encoding="utf-8"))
    row = next(
        s for s in data.get("sessions", [])
        if s.get("shpool_id") == shpool_id
    )
except (OSError, ValueError, StopIteration):
    raise SystemExit(1)
if mode == "automatic" and row.get("availability") != "ready":
    raise SystemExit(1)
identity = row.get("identity") or {}
if (
    identity.get("uuid") != uuid
    or str(identity.get("pid")) != pid_text
    or str(identity.get("process_start_ticks")) != start_text
):
    raise SystemExit(1)
if row.get("subagents"):
    raise SystemExit(1)
# Same stable-state set as the Codex bounce: refuse only mid-computation
# and unproven state.
if str(row.get("agent_status") or "").casefold() not in (
    "idle",
    "needs your reply",
    "reply optional",
):
    raise SystemExit(1)
age = row.get("recent_output_age_seconds")
if isinstance(age, int) and not isinstance(age, bool) and age < 120:
    raise SystemExit(1)
raise SystemExit(0)
PY
  python3 "$INVENTORY_CORE" platform process-is \
    "$pid" "$start" claude \
    >/dev/null 2>&1 || return 1
  local bounce_title bounce_rc
  if [[ $reason == color ]]; then
    # A recolour is not a rename. Claude reads its colour from the transcript
    # record at start or resume, so the restart is what applies it, and an
    # unnamed session needs it exactly as much as a named one -- requiring a
    # name intent here is what left unnamed sessions uncolourable. What must
    # exist is the RECORD the resumed window will read: without it the restart
    # costs the person their window and paints nothing.
    python3 "$INVENTORY_CORE" color bounce-ready claude "$uuid" >/dev/null 2>&1 || return 1
    # Whether this restart will also apply a name decides the marker below;
    # it never blocks the recolour.
    bounce_title=$(python3 "$INVENTORY_CORE" claude-bounce-title "$uuid" 2>/dev/null) ||
      bounce_title=""
  else
    bounce_title=$(python3 "$INVENTORY_CORE" claude-bounce-title "$uuid" 2>/dev/null)
    bounce_rc=$?
    if (( bounce_rc == 3 )); then
      # A real prompt followed the name intent, so the live window already
      # carries the name; the pending-boot marker is stale, never the title.
      command rm -f -- "$untitled"
      return 1
    fi
    [[ $bounce_rc -eq 0 && -n $bounce_title ]] || return 1
  fi
  sk_write_bounce_marker "$id" "$uuid" || return 1
  local recheck
  recheck=$(mktemp "$SK_STATE_DIR/bounce-recheck.json.XXXXXX") || {
    command rm -f -- "$SK_STATE_DIR/provider-bounce/$id"
    return 1
  }
  if ! sk_guard_snapshot_file "$recheck" 2>/dev/null ||
     ! python3 - "$recheck" "$id" "$uuid" "$pid" "$start" "$mode" <<'PY'
import json, sys

try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
    row = next(
        s for s in data.get("sessions", [])
        if s.get("shpool_id") == sys.argv[2]
    )
except (OSError, ValueError, StopIteration):
    raise SystemExit(1)
uuid, pid_text, start_text, mode = sys.argv[3:7]
identity = row.get("identity") or {}
if (
    identity.get("uuid") != uuid
    or str(identity.get("pid")) != pid_text
    or str(identity.get("process_start_ticks")) != start_text
):
    raise SystemExit(1)
if mode == "automatic" and row.get("availability") != "ready":
    raise SystemExit(1)
if row.get("subagents"):
    raise SystemExit(1)
if str(row.get("agent_status") or "").casefold() not in (
    "idle",
    "needs your reply",
    "reply optional",
):
    raise SystemExit(1)
# The same quiet test as the first pass, on fresh state. This recheck exists
# to catch work that STARTED during the race window, and output arriving in
# the last two minutes is exactly that evidence -- a status that still reads
# idle while the window is printing is the case this refuses.
age = row.get("recent_output_age_seconds")
if isinstance(age, int) and not isinstance(age, bool) and age < 120:
    raise SystemExit(1)
raise SystemExit(0)
PY
  then
    command rm -f -- "$recheck" "$SK_STATE_DIR/provider-bounce/$id"
    return 1
  fi
  if ! python3 "$INVENTORY_CORE" platform process-is \
    "$pid" "$start" claude \
    >/dev/null 2>&1; then
    command rm -f -- "$recheck" "$SK_STATE_DIR/provider-bounce/$id"
    return 1
  fi
  command rm -f -- "$recheck"
  if kill -TERM "$pid" 2>/dev/null; then
    # Same one-shot rule as the Codex bounce: a restart that applied no name
    # leaves the marker in place, so the pending-title remedies survive.
    if [[ -n $bounce_title ]]; then
      command rm -f -- "$untitled"
    fi
    # Whatever the reason, the window has restarted, so it has read the
    # colour the kit holds. A pending recolour is no longer pending.
    command rm -f -- "$SK_STATE_DIR/provider-recolor/$id"
    picker_action_event title_refresh provider_restarted
  else
    command rm -f -- "$SK_STATE_DIR/provider-bounce/$id"
    return 1
  fi
  return 0
}
