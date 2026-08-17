#!/usr/bin/env bash
# sp session builders: repair a broken session, pre-bake a Claude conversation,
# create a new session, and restore an exact recorded one. Source this file; do
# not execute it.
#
# Source order: bin/sp sources this module ahead of its first trap, after
# SCRIPT_DIR, session_kit_common, and INVENTORY_CORE are in place. These
# functions call the snapshot and attach helpers in lib/sh/sp_core.sh and read
# SNAPSHOT, which those helpers assign.
#
# Globals the entry script owns are assigned there, not here.
# shellcheck disable=SC2154

# Watchdog path: same repair, selected by session ID and never interactive.
# Who asked for a session. A declaration still wins -- the picker's repair
# paths pass the original session's origin, and `--origin` is an operator's
# override -- but nothing else is taken on trust.
#
# Asking a caller to volunteer "I am a machine" only works on callers that
# remember to, and agents do not: every session an agent started came back a
# person's, so the picker filled with work the operator never opened (87 and 88 were
# their examples). When nobody declares, LOOK at who is calling instead. Every
# managed session exports SHPOOL_SESSION_NAME into everything it runs, and the
# login shell the picker runs in does not export it at all. So a session
# created from inside a session was created by an agent -- whatever provider
# it starts, whatever verb it used -- and one created from the picker is the operator's.
# That reading needs no cooperation from the caller, which is the point.
sk_environment_origin() {
  local value=${SESSION_KIT_ORIGIN:-}
  if [[ $value == human || $value == machine ]]; then
    printf '%s\n' "$value"
    return 0
  fi
  # A name that is not a session ID proves nothing about the caller, so it
  # reads as a person's: unproven stays visible, the safe way to be wrong.
  if [[ ${SHPOOL_SESSION_NAME:-} =~ ^s[0-9]{8}-[0-9]{6}-[1-9][0-9]*(-[1-9][0-9]*)?$ ]]; then
    printf 'machine\n'
  else
    printf 'human\n'
  fi
}

# The origin a conversation already had, from the record written when it
# closed. A restore brings back an EXISTING session, so its origin is a fact
# to carry, not a fresh judgement about whoever runs the restore: the recovery
# sweep runs from the login shell, and reading the caller there turned every
# restored machine session back into one of the operator's rows.
#
# Prints exactly one word, and the caller decides what each one means:
#
#   machine / human   the ledger recorded this conversation's origin. It is
#                     the answer, in BOTH directions -- the record of what a
#                     session WAS outranks any reading of who is restoring it.
#   none              the ledger read, and it has never heard of this
#                     conversation. Nothing is known, so the caller reads the
#                     environment as it would for a brand new session.
#   unknown           the ledger did NOT read. That is not the same fact as
#                     "no record", and treating it as one is how an
#                     unreadable file decides a row: the caller must take the
#                     visible side.
sk_recorded_origin() {
  local provider=$1 uuid=$2
  # The library directory, not INVENTORY_CORE: the core path is redirectable,
  # and what is wanted here is the reader that owns the closed-session ledger.
  python3 - "$SCRIPT_DIR/../lib" "$provider" "$uuid" <<'PY' 2>/dev/null || printf 'unknown\n'
import os
from pathlib import Path
import sys

library, provider, uuid = sys.argv[1:4]
sys.path.insert(0, os.fspath(Path(library).resolve()))
try:
    from sessionkit_inventory import closed_sessions

    # Asked before the rows, because every reader in that module degrades an
    # unreadable ledger to "no rows" -- right for a list, wrong for deciding
    # whose session this is.
    if not closed_sessions.ledger_is_readable():
        print("unknown")
        raise SystemExit(0)
    # Unfiltered on purpose. The picker's list drops conversations whose
    # transcript this machine can no longer read, because it cannot promise to
    # restore them -- but a restore is already under way here, and whether the
    # session was a machine's is a fact about what it WAS, not about whether
    # its transcript survives.
    rows = closed_sessions.load_closed()
except (ImportError, OSError, ValueError):
    # The ledger is the thing that says a session was an agent's. Answering
    # "no record" when it could not be read is what let an unreadable file
    # launder an agent's session into the person's list, and the same silence
    # in the other direction would hide one of the person's own.
    print("unknown")
    raise SystemExit(0)
if not isinstance(rows, list):
    print("unknown")
    raise SystemExit(0)
for row in rows:
    if not isinstance(row, dict) or row.get("provider") != provider:
        continue
    if str(row.get("uuid") or "").casefold() != uuid.casefold():
        continue
    origin = row.get("origin")
    print(origin if origin in ("human", "machine") else "unknown")
    raise SystemExit(0)
print("none")
PY
}

# Stamping is advisory: a session that could not be stamped is listed as a
# person's, which is the safe direction, and says so once.
sk_record_origin() {
  # A collision retry has only a fresh manager name, not a verified process
  # generation. The shared attach helper still offers that two-argument
  # callback for the older stamping contract; defer it, because every creator
  # below records the origin after exact generation capture.
  if (( $# == 2 )); then
    return 0
  fi
  (( $# == 5 )) || return 1
  local id=$1 origin=$2 started_at=$3 shell_pid=$4 shell_start_ticks=$5
  [[ $origin == human || $origin == machine ]] || origin=human
  SESSION_KIT_ORIGIN_STARTED_AT_UNIX_MS=$started_at \
    SESSION_KIT_ORIGIN_SHELL_PID=$shell_pid \
    SESSION_KIT_ORIGIN_SHELL_START_TICKS=$shell_start_ticks \
    python3 "$INVENTORY_CORE" origin record "$id" "$origin" >/dev/null 2>&1 && return 0
  [[ $origin == human ]] ||
    printf 'session-kit: the session could not be stamped as a machine session, so it is listed as yours.\n' >&2
  return 0
}

repair_target() {
  local selector=$1
  picker_action_event repair requested
  sk_require_integration || {
    picker_action_event repair refused
    return 1
  }
  make_guard_snapshot || {
    picker_action_event repair refused
    return 1
  }
  sk_resolve "$SNAPSHOT" "$selector" || {
    sk_die "no session matches that selector"
    return 2
  }
  sk_require_mutation_target || return 1
  [[ $SK_PROVIDER == claude || $SK_PROVIDER == codex ]] || {
    sk_die "only a Claude or Codex session can be repaired"
    return 1
  }
  local id=$SK_ID provider=$SK_PROVIDER uuid=$SK_UUID cwd=$SK_CWD started=$SK_STARTED
  local shell_pid=$SK_SHELL_PID shell_start=$SK_SHELL_START
  local provider_pid=$SK_PROVIDER_PID provider_start=$SK_PROVIDER_START
  local daemon_pid=$SK_DAEMON_PID daemon_start=$SK_DAEMON_START
  [[ $uuid =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || {
    sk_die "repair requires a canonical conversation UUID"
    return 1
  }
  [[ $cwd == /* && -d $cwd ]] || {
    sk_die "session directory is not an existing absolute directory"
    return 1
  }
  cwd=$(cd -- "$cwd" && pwd -P) || return 1
  picker_lock || {
    picker_action_event repair refused
    return 1
  }
  sk_revalidate "$id" "$started" "$provider" "$uuid" \
    "$shell_pid" "$shell_start" "$provider_pid" "$provider_start" \
    "$daemon_pid" "$daemon_start" || {
    sk_lock_release 9
    sk_die "session changed while preparing the repair; nothing changed"
    picker_action_event repair state_changed
    return 1
  }
  if [[ $SK_STATUS != disconnected && $SK_STATUS != Disconnected ]]; then
    sk_lock_release 9
    sk_die "session is open in another window; repair refused and nothing changed"
    picker_action_event repair attached
    return 1
  fi
  if ! sk_shpool_holder_is "$daemon_pid" "$daemon_start"; then
    sk_lock_release 9
    sk_die "socket holder changed before repair close; no kill was sent and nothing changed"
    picker_action_event repair state_changed
    return 1
  fi
  local shell_outcome
  if ! shell_outcome=$(sk_terminate_exact_shell "$daemon_pid" "$daemon_start" \
      "$shell_pid" "$shell_start" 2>&1); then
    sk_cleanup_dead_shpool_entry "$id" "$daemon_pid" "$daemon_start" \
      "$shell_pid" "$shell_start" "$started" || true
    sk_lock_release 9
    sk_die "exact repair shell PID $shell_pid start $shell_start was not terminated ($shell_outcome); automatic repair stopped before leftover cleanup"
    picker_action_event repair failed
    return 1
  fi
  sk_cleanup_dead_shpool_entry "$id" "$daemon_pid" "$daemon_start" \
    "$shell_pid" "$shell_start" "$started" || {
      sk_lock_release 9
      return 1
    }
  sk_lock_release 9
  if finish_recovery_after_kill "$id" "$provider" "$uuid" "$cwd" \
    "$shell_pid" "$shell_start" "$provider_pid" "$provider_start"; then
    picker_action_event repair recovered
    return 0
  fi
  picker_action_event repair failed
  echo "session-kit: the session was already closed. Its conversation is intact; restore it from the picker." >&2
  return 1
}

# Whether the throwaway has written the color record we asked it for. Cheap
# enough to ask ten times a second; the authoritative JSON check runs once,
# afterwards, in sk_prebake_claude.
sk_prebake_color_landed() {
  local transcript=$1 color=$2
  [[ -s $transcript ]] || return 1
  grep -as -e '"agent-color"' -- "$transcript" 2>/dev/null |
    grep -qas -e "\"$color\"" 2>/dev/null
}

# Every process still carrying this conversation ID, gone. The ID was minted
# seconds ago and appears in nothing else on the machine, so this can only
# match the throwaway pair this function started -- never a real session.
sk_prebake_reap() {
  local minted=$1
  [[ $minted =~ ^[0-9a-fA-F-]{36}$ ]] || return 0
  python3 - "$minted" <<'PY' >/dev/null 2>&1 || true
import os
import signal
import sys
import time

needle = sys.argv[1].encode()
me = os.getpid()


def targets():
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == me:
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as handle:
                argv = handle.read()
        except OSError:
            continue
        if needle in argv and (b"claude" in argv or b"script" in argv):
            found.append(int(entry))
    return found


# SIGTERM first so the provider closes its transcript cleanly; SIGKILL only
# for whatever ignores it.
for number in (signal.SIGTERM, signal.SIGKILL):
    pids = targets()
    if not pids:
        break
    for pid in pids:
        try:
            os.kill(pid, number)
        except OSError:
            pass
    for _ in range(20):
        time.sleep(0.1)
        if not targets():
            break
PY
}

# End an in-flight pre-bake, from a signal handler or an exit trap. Safe to
# call at any time: it does nothing unless a throwaway is actually running.
# `sp` installs it on INT, TERM, HUP and EXIT, because a Ctrl-C tears the
# shell down before sk_prebake_claude can reach its own reap.
sk_prebake_abort() {
  local pending=${SK_PREBAKE_IN_FLIGHT:-}
  [[ -n $pending ]] || return 0
  SK_PREBAKE_IN_FLIGHT=
  sk_prebake_reap "$pending"
  python3 "$INVENTORY_CORE" color conversation-release claude "$pending" \
    "${SK_PREBAKE_COLOR:-}" >/dev/null 2>&1 || true
  return 0
}

# Drive the throwaway TUI's stdin, and stop the moment its color record
# exists.
#
# Order matters here, and getting it backwards cost the whole feature once.
# This waited for the throwaway's TRANSCRIPT FILE to appear before it typed
# anything -- but real Claude Code does not create that file until it writes
# its first record, and in a throwaway the first record is the one `/color`
# itself produces. So the feeder was waiting for a file that only its own
# keystrokes could create: it burned the entire deadline, returned without
# ever typing, and every Claude session paid ~16 seconds of silence for a
# colour that never landed. Measured against claude 2.1.233, typing first and
# then polling: the transcript appears ~0.02s after the keystrokes and the
# colour record ~0.03s after that.
#
# So: settle, type, and only then look for the record.
sk_prebake_feed() {
  local color=$1 root=$2 minted=$3 deadline=$4
  local transcript= waited=0 sends=0 settle=0
  local limit=$((deadline * 10))
  # Tenths of a second before the first keystroke. The TUI puts the terminal
  # into raw mode as it comes up and that switch discards input already
  # queued, so anything typed before it is simply gone. Measured with a single
  # send and no re-send: 0.5s never landed, 1.0s was eaten once in three, 1.5s
  # landed every time. Hence this settle AND the bounded re-send below.
  local first=${SESSION_KIT_PREBAKE_SETTLE:-15}
  [[ $first =~ ^[0-9]+$ ]] && ((first >= 1 && first < limit)) || first=15
  while ((waited < first)); do
    sleep 0.1 2>/dev/null || return 0
    ((waited++))
  done
  while ((waited < limit)); do
    # Re-send ONLY while the throwaway has still written nothing. A transcript
    # is proof the keystrokes arrived; typing a second `/color` on top of an
    # input line the TUI is already holding is the one way a re-send could
    # build a line that is not a slash command, and the trailing carriage
    # return would then SUBMIT it as a prompt on the person's own account.
    if [[ -z $transcript ]] && ((sends < 6)); then
      printf '/color %s\r' "$color"
      ((sends++))
    fi
    settle=0
    while ((settle < 15 && waited < limit)); do
      sleep 0.1 2>/dev/null || return 0
      ((waited++))
      ((settle++))
      if [[ -z $transcript ]]; then
        transcript=$(compgen -G "$root/projects/*/$minted.jsonl" 2>/dev/null | head -1)
      fi
      if [[ -n $transcript ]] && sk_prebake_color_landed "$transcript" "$color"; then
        # Done. Closing stdin is not enough -- the real TUI keeps running --
        # so the throwaway is ended here rather than by the timeout.
        sk_prebake_reap "$minted"
        return 0
      fi
    done
    # A throwaway that has already exited is never going to write one. Waiting
    # out the whole deadline for it costs the person the same silence as a
    # hang -- a provider that cannot start (no credentials, wrong profile)
    # should cost the seconds it took to fail, not the ceiling.
    if command -v pgrep >/dev/null 2>&1 &&
       ! pgrep -f -- "$minted" >/dev/null 2>&1; then
      return 0
    fi
  done
  return 0
}

# First-window color: boot a hidden throwaway TUI on a minted conversation ID,
# let it write its own native color record by typing /color into ITSELF (no
# human terminal is ever touched), then launch the real session as a resume of
# that conversation so the color is native from the first frame. Fail-open:
# any problem falls back to the plain new-session flow. Kill switches:
# SESSION_KIT_NO_PREBAKE=1 or $SK_STATE_DIR/prebake-off.
sk_prebake_claude() {
  SK_PREBAKE_UUID=
  # Three outcomes, told apart because they mean different things to the
  # person: `off` is a choice (kill switch, or no tooling to do it with) and
  # says nothing; `failed` means the kit tried to give this window its color
  # from the first frame and could not, which the caller reports; `ready`
  # worked.
  SK_PREBAKE_STATUS=off
  [[ ${SESSION_KIT_NO_PREBAKE:-0} != 1 && ! -e $SK_STATE_DIR/prebake-off ]] ||
    return 1
  command -v script >/dev/null 2>&1 || return 1
  command -v claude >/dev/null 2>&1 || return 1
  SK_PREBAKE_STATUS=failed
  # An account session pre-bakes inside ITS OWN profile. This step used to be
  # skipped outright whenever an account was named, which is why a session on
  # a chosen profile opened with no colour while a default-profile one opened
  # with its colour already in the first frame. The throwaway conversation has
  # to be minted where the real session will resume it: one written to the
  # ambient profile is invisible to a session that runs with CLAUDE_CONFIG_DIR
  # set, so the resume would fail and take the colour with it.
  #
  # With no account named, the throwaway inherits whatever profile this shell
  # already runs under, so the root to look in is that one -- not a hardcoded
  # $HOME/.claude. An operator who sets CLAUDE_CONFIG_DIR himself (a
  # documented setup, not an edge case) previously had the throwaway write
  # into THEIR profile while the kit looked in $HOME/.claude, found nothing, and
  # reported the step as failed after paying for all of it.
  local prebake_root=${CLAUDE_CONFIG_DIR:-$HOME/.claude}
  # Pinned per command, never exported: an `export` here survived every later
  # `return 1` and left the ambient profile of the whole rest of `sp new` --
  # color propagate, account bind, snapshot, attach -- pointing at one
  # account's directory.
  local -a prebake_env=(-u CLAUDE_CODE_CHILD_SESSION -u CLAUDE_CODE_SESSION_ID)
  if [[ -n ${requested_account:-} ]]; then
    # `account launch-profile` already ran and was validated above, so the
    # profile is read from that answer rather than asked for a second time.
    local prebake_profile
    prebake_profile=$(python3 -c 'import json,sys
value = json.loads(sys.stdin.read())
profile = value.get("profile_dir")
if not isinstance(profile, str) or not profile.startswith("/"):
    raise SystemExit(1)
print(profile)' <<<"${account_profile_json:-}" 2>/dev/null) || return 1
    [[ -d $prebake_profile && ! -L $prebake_profile ]] || return 1
    prebake_env+=("CLAUDE_CONFIG_DIR=$prebake_profile")
    prebake_root=$prebake_profile
  fi
  # This runs BEFORE any other creation output. TTY-gated: background callers
  # keep clean output.
  [[ -t 1 ]] &&
    printf 'Starting a Claude session. This takes a few seconds.\n'
  local minted color transcript
  minted=$(python3 -c 'import uuid; print(uuid.uuid4())' 2>/dev/null) || return 1
  color=$(python3 "$INVENTORY_CORE" color conversation-pick claude "$minted" \
    2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["color"])') || return 1
  # The ceiling, not the cost: the feeder stops as soon as the record lands.
  local deadline=${SESSION_KIT_PREBAKE_DEADLINE:-14}
  [[ $deadline =~ ^[0-9]+$ ]] && ((deadline >= 3 && deadline <= 60)) || deadline=14
  # Named for `sk_prebake_abort`, which `sp`'s signal traps call: a Ctrl-C
  # ends this shell before the reap below can be reached.
  SK_PREBAKE_IN_FLIGHT=$minted
  SK_PREBAKE_COLOR=$color
  ( cd "$cwd" 2>/dev/null &&
    # A Ctrl-C while the person waits for this must take the throwaway with
    # it, and so must a hard kill of this shell.
    export SK_TIMEOUT_TIE_CHILD=1 &&
    sk_prebake_feed "$color" "$prebake_root" "$minted" "$deadline" |
      if [[ $(sk_platform) == Darwin ]]; then
        sk_timeout "$deadline" env "${prebake_env[@]}" \
          script -q /dev/null claude --session-id "$minted"
      else
        sk_timeout "$deadline" env "${prebake_env[@]}" \
          script -qec "claude --session-id $minted" /dev/null
      fi
  ) >/dev/null 2>&1 || true
  # Belt and braces: whatever happened above -- landed, timed out, interrupted
  # -- nothing carrying this conversation ID is left running. A four-day-old
  # throwaway pair was found alive on the operator box holding ~250 MB and an
  # account profile, with nothing on the machine able to reap it.
  sk_prebake_reap "$minted"
  SK_PREBAKE_IN_FLIGHT=
  if ! transcript=$(compgen -G "$prebake_root/projects/*/$minted.jsonl" | head -1) ||
     [[ -z $transcript || ! -s $transcript || -L $transcript ]]; then
    python3 "$INVENTORY_CORE" color conversation-release claude "$minted" "$color" \
      >/dev/null 2>&1 || true
    return 1
  fi
  if ! python3 - "$transcript" "$color" <<'PY' >/dev/null 2>&1
import json, sys
path, expected = sys.argv[1:]
matched = False
# Bytes, decoded per line: the throwaway was just stopped, so its last record
# can be half-written, and text mode raises UnicodeDecodeError from the
# iteration itself -- which would fail a prebake that actually worked.
with open(path, "rb") as stream:
    for line in stream:
        try:
            item = json.loads(line.decode("utf-8", "strict"))
        except (UnicodeDecodeError, ValueError):
            continue
        if not isinstance(item, dict):
            continue
        if item.get("type") == "agent-color" and item.get("agentColor") == expected:
            matched = True
raise SystemExit(0 if matched else 1)
PY
  then
    python3 "$INVENTORY_CORE" color conversation-release claude "$minted" "$color" \
      >/dev/null 2>&1 || true
    return 1
  fi
  SK_PREBAKE_UUID=$minted
  SK_PREBAKE_STATUS=ready
}

# Copy a private prompt into the paired-launch namespace. The source remains in
# place until the separate audit append succeeds, so an audit failure can never
# erase both copies.
sk_stage_prompt_handoff() {
  local source=$1 destination=$2
  SK_STAGED_PROMPT_IDENTITY=$(python3 - "$source" "$destination" <<'PY'
import os
from pathlib import Path
import stat
import sys

source, destination = map(Path, sys.argv[1:])
if not source.is_absolute():
    source = Path.cwd() / source
flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)

def open_parent(path, *, create=False, private=False):
    if not path.is_absolute() or ".." in path.parts:
        raise OSError("unsafe prompt path")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = path.parent.parts[1:]
        for index, component in enumerate(parts):
            try:
                child = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, 0o700, dir_fd=descriptor)
                os.fsync(descriptor)
                child = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
            info = os.fstat(child)
            if not stat.S_ISDIR(info.st_mode):
                os.close(child)
                raise OSError("unsafe prompt ancestor")
            if private and index == len(parts) - 1 and (
                info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700
            ):
                os.close(child)
                raise OSError("unsafe prompt handoff directory")
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise

source_parent = open_parent(source)
try:
    source_fd = os.open(source.name, flags, dir_fd=source_parent)
except OSError:
    os.close(source_parent)
    raise SystemExit(1)
destination_fd = -1
destination_parent = -1
try:
    info = os.fstat(source_fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
        or not 0 < info.st_size <= 1024 * 1024
    ):
        raise OSError("unsafe prompt")
    payload = os.read(source_fd, info.st_size + 1)
    if len(payload) != info.st_size or b"\0" in payload or b"\x1b" in payload:
        raise OSError("unsafe prompt bytes")
    payload.decode("utf-8")
    destination_parent = open_parent(destination, create=True, private=True)
    create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    create_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    destination_fd = os.open(destination.name, create_flags, 0o600, dir_fd=destination_parent)
    os.fchmod(destination_fd, 0o600)
    written = 0
    while written < len(payload):
        written += os.write(destination_fd, payload[written:])
    os.fsync(destination_fd)
    destination_info = os.fstat(destination_fd)
    if destination_info.st_size != len(payload):
        raise OSError("short prompt write")
    os.fsync(destination_parent)
    path_info = os.stat(source.name, dir_fd=source_parent, follow_symlinks=False)
    if (path_info.st_dev, path_info.st_ino) != (info.st_dev, info.st_ino):
        raise OSError("prompt pathname changed")
    print(f"{info.st_dev}:{info.st_ino}")
except (OSError, UnicodeError):
    try:
        if destination_parent >= 0:
            os.unlink(destination.name, dir_fd=destination_parent)
    except OSError:
        pass
    raise SystemExit(1)
finally:
    if destination_fd >= 0:
        os.close(destination_fd)
    if destination_parent >= 0:
        os.close(destination_parent)
    os.close(source_fd)
    os.close(source_parent)
PY
) || return 1
  [[ $SK_STAGED_PROMPT_IDENTITY =~ ^[0-9]+:[0-9]+$ ]]
}

sk_finalize_prompt_source() {
  local source=$1 identity=$2
  python3 - "$source" "$identity" <<'PY'
import os
from pathlib import Path
import sys

source = Path(sys.argv[1])
expected = tuple(map(int, sys.argv[2].split(":")))
info = source.lstat()
if (info.st_dev, info.st_ino) != expected:
    raise SystemExit(1)
source.unlink()
directory = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY
}

sk_abort_prompt_stage() {
  local source=$1 destination=$2 identity=$3
  python3 - "$source" "$destination" "$identity" <<'PY'
import os
from pathlib import Path
import sys

source, destination = map(Path, sys.argv[1:3])
expected = tuple(map(int, sys.argv[3].split(":")))
try:
    info = source.lstat()
except OSError:
    raise SystemExit(1)
if (info.st_dev, info.st_ino) != expected:
    raise SystemExit(1)
destination.unlink()
directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY
}

sk_write_launch_request() {
  local shpool_id=$1 provider=$2 cwd=$3 model=$4 launch_key=$5
  [[ -n $model && $provider =~ ^(claude|codex)$ && $cwd == /* ]] || return 1
  [[ $model != *[$'\t\r\n\034']* && $launch_key != *[$'\t\r\n\034']* ]] || return 1
  [[ -z $launch_key || $launch_key =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$ ]] || return 1
  [[ ! -e $SK_START_DIR/$shpool_id.launch && ! -L $SK_START_DIR/$shpool_id.launch ]] || return 1
  local temporary="$SK_START_DIR/.${shpool_id}.launch.$$"
  printf '%s\t%s\t%s\t%s\n' "$provider" "$cwd" "$model" "$launch_key" > "$temporary" || return 1
  chmod 600 "$temporary" 2>/dev/null || true
  if ! mv -f -- "$temporary" "$SK_START_DIR/$shpool_id.launch"; then
    [[ ! -e $temporary ]] || command rm -- "$temporary"
    return 1
  fi
}

sk_arm_launch_request() {
  local shpool_id=$1 provider=$2 cwd=$3 model=$4 launch_key=$5
  local boot_id=$6 started=$7 shell_pid=$8 shell_start=$9
  local daemon_pid=${10} daemon_start=${11}
  local path="$SK_START_DIR/$shpool_id.launch"
  [[ -f $path && ! -L $path ]] || return 1
  local request_provider request_cwd request_model request_key extra request_line
  IFS= read -r request_line < "$path" || return 1
  # A tab is IFS whitespace, so a run of them reads as ONE separator and an
  # empty field vanishes -- the launch key is empty for every launch nobody
  # passed --launch-key to. Read on \034, which is not whitespace, and every
  # field keeps its place. (The husk bug: three launches died of this.)
  request_line=${request_line//$'\t'/$'\034'}
  IFS=$'\034' read -r request_provider request_cwd request_model request_key extra \
    <<<"$request_line"
  [[ -z $extra && $request_provider == "$provider" && $request_cwd == "$cwd" &&
     $request_model == "$model" && $request_key == "$launch_key" ]] || return 1
  local temporary="$SK_START_DIR/.${shpool_id}.launch-armed.$$"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$provider" "$cwd" "$model" "$launch_key" "$boot_id" "$started" \
    "$shell_pid" "$shell_start" "$daemon_pid" "$daemon_start" > "$temporary" || return 1
  chmod 600 "$temporary" 2>/dev/null || true
  if ! mv -f -- "$temporary" "$path"; then
    [[ ! -e $temporary ]] || command rm -- "$temporary"
    return 1
  fi
}

sk_quarantine_launch_request() {
  local shpool_id=$1 destination=$2
  local path="$SK_START_DIR/$shpool_id.launch"
  [[ ! -e $path && ! -L $path ]] && return 0
  [[ -f $path && ! -L $path ]] || return 1
  mv -- "$path" "$destination.launch"
}

start_new() {
  sk_require_integration || return 1
  local requested_provider= requested_project= prompt_file= requested_model= launch_key=
  local requested_account= account_profile_json= requested_worktree=
  local refuse_worktree=0 auto_worktree=0 model_anyway=0
  # Unstamped is a person's: a tool that forgets to say it is a machine shows
  # up in the human list and gets noticed, rather than hiding by accident.
  # SESSION_KIT_ORIGIN lets an automation stamp everything it starts without
  # threading a flag through every call site.
  local requested_origin=
  requested_origin=$(sk_environment_origin)
  local -a positional=()
  while (($#)); do
    case "$1" in
      --prompt-file)
        [[ -z $prompt_file && -n ${2:-} ]] || {
          sk_die "--prompt-file requires one file"
          return 2
        }
        prompt_file=$2
        shift 2
        ;;
      --model)
        [[ -z $requested_model && -n ${2:-} ]] || {
          sk_die "--model requires one model identifier"
          return 2
        }
        requested_model=$2
        shift 2
        ;;
      --launch-key)
        [[ -z $launch_key && -n ${2:-} ]] || {
          sk_die "--launch-key requires one idempotency key"
          return 2
        }
        launch_key=$2
        shift 2
        ;;
      --account)
        [[ -z $requested_account && -n ${2:-} ]] || {
          sk_die "--account requires one enrolled account alias"
          return 2
        }
        requested_account=$2
        shift 2
        ;;
      --origin)
        # Who is asking. A machine says so; a person never has to.
        [[ -n ${2:-} && ( $2 == human || $2 == machine ) ]] || {
          sk_die "--origin requires human or machine"
          return 2
        }
        requested_origin=$2
        shift 2
        ;;
      --worktree)
        [[ -z $requested_worktree && -n ${2:-} ]] || {
          sk_die "--worktree requires one branch name"
          return 2
        }
        requested_worktree=$2
        shift 2
        ;;
      --no-worktree)
        # Delegated work gets its own copy by default. This is how a machine
        # session says it means to work in the checkout itself.
        refuse_worktree=1
        shift
        ;;
      --model-anyway)
        # The person was told what this machine would really serve and chose
        # the model they asked for regardless. Never a substitution either way.
        model_anyway=1
        shift
        ;;
      --*) sk_die "unknown new-session option: $1"; return 2 ;;
      *) positional+=("$1"); shift ;;
    esac
  done
  (( ${#positional[@]} <= 2 )) || {
    sk_die "new accepts at most a provider and project alias"
    return 2
  }
  requested_provider=${positional[0]:-}
  requested_project=${positional[1]:-}
  local provider=shell cwd=$PWD project_row=""

  if [[ $requested_provider =~ ^(claude|codex|shell)$ ]]; then
    provider=$requested_provider
  elif [[ -n $requested_provider ]]; then
    requested_project=$requested_provider
    requested_provider=""
  fi

  if [[ -n $requested_project ]]; then
    # A project carries more than a directory now: a committed session-kit.toml
    # can name the provider, account, and model this project is worked on with,
    # so `sp new <project>` starts what the project says instead of what the
    # person happened to remember. Flags passed here still win, and the
    # account and model returned go through the same validation below as one
    # typed on the command line.
    project_row=$(sk_project_plan "$requested_project" "$requested_provider" \
      "$requested_account" "$requested_model") || {
      sk_die "unknown or invalid project: $requested_project"
      return 1
    }
    local planned_cwd planned_provider planned_account planned_model startup_state
    # Same husk: a project that names no account and no model sends two empty
    # fields, tab-collapse shifts the rest left, and `sp new <project>` refused
    # itself with "the selected account could not be prepared" while the
    # startup notice never printed at all.
    project_row=${project_row//$'\t'/$'\034'}
    IFS=$'\034' read -r planned_cwd planned_provider planned_account planned_model \
      startup_state <<<"$project_row"
    [[ -n $planned_cwd ]] || {
      sk_die "the project plan for $requested_project was incomplete"
      return 1
    }
    # A project is a directory, not a provider. One with no default provider
    # says which words open it instead of falling through to a shell nobody
    # asked for.
    [[ -n $planned_provider ]] || {
      sk_die "$requested_project has no default provider; name one: sp new claude $requested_project, sp new codex $requested_project, or sp new shell $requested_project"
      return 2
    }
    cwd=$planned_cwd
    provider=$planned_provider
    [[ -n $requested_account || -z $planned_account ]] || requested_account=$planned_account
    [[ -n $requested_model || -z $planned_model ]] || requested_model=$planned_model
    case "$startup_state" in
      unapproved)
        printf "This project's startup command is not approved here, so it was not run.\n"
        printf 'Review it with: session-kit projects launch-plan %s\n' "$requested_project"
        printf 'Approve it with: session-kit projects approve-startup %s\n' "$requested_project"
        ;;
      changed)
        printf "This project's startup command changed since it was approved, so it was not run.\n"
        printf 'Review it with: session-kit projects launch-plan %s\n' "$requested_project"
        printf 'Approve it with: session-kit projects approve-startup %s\n' "$requested_project"
        ;;
    esac
  fi

  [[ -d $cwd ]] || {
    sk_die "project directory does not exist: $cwd"
    return 1
  }
  cwd=$(cd -- "$cwd" && pwd -P) || return 1

  # Delegated work gets its own copy of the code. A machine session standing in
  # a git repository is given a worktree on a branch of its own -- it does not
  # have to ask, and two workers on one project stop editing the same files at
  # the same time. `--no-worktree` is how an automation says it means to work
  # in the checkout itself; a directory that is not a repository has nothing to
  # isolate and says so rather than refusing to start.
  if [[ -z $requested_worktree && $refuse_worktree == 0 &&
        $requested_origin == machine ]]; then
    local shared_reason= copy_check_status=0
    if ! command git -C "$cwd" rev-parse --show-toplevel >/dev/null 2>&1; then
      printf '%s is not a git repository, so this session works in it directly.\n' \
        "$cwd"
    else
      # `copy-check` answers in exit codes: 0 with a reason means do not copy
      # this one, 1 means copying it is fine. Anything else is the check
      # failing, and a failed guard is not a pass -- reading "non-zero" as "go
      # ahead" is how a session ends up editing a copy of a checkout whose
      # files are the running site. Three states, three branches, and the
      # unknown one takes the same road as the refusal.
      shared_reason=$(python3 "$INVENTORY_CORE" worktree copy-check --repo "$cwd") ||
        copy_check_status=$?
      if (( copy_check_status == 0 )); then
        # Two reasons not to copy a repository without being asked: it is the
        # running thing (a directory whose files are served live, where a copy
        # is work that never takes effect), or it is big enough that copying it
        # is a decision somebody should make on purpose. Either way the session
        # runs in the checkout and says which.
        printf 'This session works in %s itself: %s.\n' "$cwd" \
          "${shared_reason:-it is not copied without being asked}"
        printf 'Give it a copy of its own with --worktree BRANCH.\n'
      elif (( copy_check_status == 1 )); then
        local worktree_stamp=
        worktree_stamp=$(sk_now_unix_ms) || return 1
        requested_worktree="sk/w-$worktree_stamp-$$"
        auto_worktree=1
      else
        printf 'session-kit: could not check whether %s is safe to copy, so this session works in it directly.\n' \
          "$cwd"
        printf 'Give it a copy of its own with --worktree BRANCH.\n'
      fi
    fi
  fi

  # Worktree isolation: the branch is already decided elsewhere -- a delegated
  # worker's plan records it, a person names it here, or the line above cut one
  # for a machine session -- and this materializes it as a directory of its own.
  # Idempotent by (repository, branch), so a retried launch reuses the worktree
  # instead of forking a second one.
  if [[ -n $requested_worktree ]]; then
    local worktree_record= worktree_path=
    local -a materialize_argv=(python3 "$INVENTORY_CORE" worktree materialize
      --repo "$cwd" --branch "$requested_worktree")
    # Only a copy the kit chose is a copy the kit gives back on close. A branch
    # a person named stays until they say otherwise.
    (( auto_worktree == 0 )) ||
      materialize_argv+=(--auto --origin "$requested_origin")
    worktree_record=$("${materialize_argv[@]}") || {
      sk_die "no worktree for branch $requested_worktree; no session was created"
      return 1
    }
    worktree_path=$(printf '%s' "$worktree_record" | python3 -c '
import json,sys
record = json.load(sys.stdin)
path = record.get("path") or ""
if not isinstance(path, str) or not path.startswith("/"):
    raise SystemExit(1)
print(path)
') || {
      sk_die "the worktree record did not name an absolute directory"
      return 1
    }
    [[ -d $worktree_path ]] || {
      sk_die "the recorded worktree directory is missing: $worktree_path"
      return 1
    }
    cwd=$(cd -- "$worktree_path" && pwd -P) || return 1
  fi

  if [[ -n $prompt_file && $provider != codex ]]; then
    sk_die "--prompt-file is available only for a new Codex session"
    return 2
  fi
  if [[ -n $requested_account ]]; then
    [[ $provider =~ ^(claude|codex)$ &&
       $requested_account =~ ^[a-z][a-z0-9_-]{0,11}$ ]] || {
      sk_die "--account is available only for Claude or Codex with an enrolled alias"
      return 2
    }
    local account_error
    account_error=$(mktemp) || return 1
    account_profile_json=$(python3 "$INVENTORY_CORE" account launch-profile \
      "$provider" "$requested_account" 2>"$account_error") || {
      # The core names the failed predicate (blocked state, stale feed,
      # missing enrollment). Print that truth, never a guessed login problem.
      sk_die "$(grep -m1 . "$account_error" 2>/dev/null ||
        echo "the selected $provider account could not be prepared")"
      command rm -f -- "$account_error"
      return 1
    }
    command rm -f -- "$account_error"
    python3 -c '
import json,sys
v=json.loads(sys.stdin.read())
raise SystemExit(0 if v.get("provider")==sys.argv[1] and v.get("alias")==sys.argv[2]
                 and isinstance(v.get("profile_dir"),str) and v["profile_dir"].startswith("/") else 1)
' "$provider" "$requested_account" <<<"$account_profile_json" || {
      sk_die "the selected account profile returned an invalid launch identity"
      return 1
    }
  fi
  if [[ -n $requested_model ]]; then
    [[ $provider =~ ^(claude|codex)$ ]] || {
      sk_die "--model is available only for Claude or Codex"
      return 2
    }
    local validated_model
    validated_model=$(python3 "$INVENTORY_CORE" validate-worker-model \
      "$provider" "$requested_model" 2>/dev/null) || {
        sk_die "unsupported or unsafe $provider model identifier"
        return 2
      }
    [[ $validated_model == "$requested_model" ]] || {
      sk_die "model validation did not preserve the identifier"
      return 2
    }
    # The identifier is well formed. Whether this machine actually answers it
    # with that model is a different question, and it is asked here -- before
    # the session exists -- because a session that quietly runs on something
    # else runs on it for the whole conversation. Nothing is substituted: the
    # refusal names what would serve the request, and --model-anyway is how a
    # person says they meant it.
    if (( model_anyway == 0 )); then
      local availability_message= availability_status=0
      availability_message=$(python3 "$INVENTORY_CORE" model-availability \
        "$provider" "$requested_model" --flag=--model-anyway 2>&1 >/dev/null) ||
        availability_status=$?
      if (( availability_status == 3 )); then
        [[ -z $availability_message ]] || printf '%s\n' "$availability_message" >&2
        sk_die "no session was created"
        return 2
      fi
      if (( availability_status != 0 )); then
        # The check itself could not run. That is not evidence the model is
        # wrong, and it is never evidence it is right: say which -- including
        # whatever the check itself said -- and start exactly what was asked.
        [[ -z $availability_message ]] || printf '%s\n' "$availability_message" >&2
        printf 'session-kit: could not check whether this machine serves %s; starting it as asked.\n' \
          "$requested_model" >&2
      elif [[ -n $availability_message && -t 2 ]]; then
        # A verdict of "unknown" arrives here: nothing on this machine can
        # confirm the model, which is not a refusal and never stops a launch.
        # A person is told, once, at the moment they asked. A scripted or
        # delegated launch is not: repeating "nobody has confirmed this" into
        # every log line is how a real warning stops being read. Anything can
        # ask for the verdict outright with `session-kit model-availability`.
        printf '%s\n' "$availability_message" >&2
      fi
    fi
  elif [[ -n $launch_key ]]; then
    sk_die "--launch-key requires --model"
    return 2
  fi
  if [[ -n $launch_key ]]; then
    launch_key=$(python3 - "$launch_key" <<'PY'
import re
import sys
value = sys.argv[1].strip()
if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) is None:
    raise SystemExit(1)
print(value)
PY
    ) || {
      sk_die "--launch-key is not a supported idempotency key"
      return 2
    }
  fi

  local launch_uuid= launch_mode=new
  if [[ $provider == claude ]] && sk_prebake_claude; then
    # The window opens as a resume of the pre-baked conversation, so the
    # session color is already native in its very first frame.
    launch_uuid=$SK_PREBAKE_UUID
    launch_mode=resume
  elif [[ ${SK_PREBAKE_STATUS:-off} == failed ]]; then
    # It was attempted and did not finish. The session still opens; its color
    # arrives at the next start instead of the first frame, and saying so
    # beats a window that quietly does not match the picker.
    printf 'The first-frame color step did not finish; this window takes its color from its next start.\n' >&2
  fi
  sk_prepare_state || return 1
  exec 9>"$SK_STATE_DIR/create.lock" || return 1
  sk_lock_acquire 9 "$SK_STATE_DIR/create.lock" || { exec 9>&-; return 1; }
  if ! sk_require_integration; then
    sk_lock_release 9
    return 1
  fi
  local id
  id=$(sk_allocate_id) || {
    sk_lock_release 9
    sk_die "could not allocate a unique session ID"
    return 1
  }
  local prompt_handoff=
  if [[ -n $prompt_file ]]; then
    prompt_handoff=$SK_START_DIR/$id.prompt
    if ! sk_stage_prompt_handoff "$prompt_file" "$prompt_handoff"; then
      sk_lock_release 9
      sk_die "prompt file must be a readable owner-private mode-0600 regular UTF-8 file"
      sk_log_action prompt_handoff refused || true
      return 1
    fi
    if ! sk_log_action prompt_handoff created; then
      sk_abort_prompt_stage "$prompt_file" "$prompt_handoff" \
        "$SK_STAGED_PROMPT_IDENTITY" >/dev/null 2>&1 || true
      sk_lock_release 9
      sk_die "prompt handoff could not be audited; no session was created and at least one private copy was retained"
      return 1
    fi
  fi
  sk_write_start_record "$id" "$provider" "$cwd" "$launch_uuid" "$launch_mode" || {
    [[ -z $prompt_handoff ]] ||
      sk_abort_prompt_stage "$prompt_file" "$prompt_handoff" \
        "$SK_STAGED_PROMPT_IDENTITY" >/dev/null 2>&1 || true
    sk_lock_release 9
    return 1
  }
  if [[ -n $requested_account ]] &&
     ! sk_write_account_record "$id" "$provider" "$requested_account"; then
    command rm -f -- "$SK_START_DIR/$id" "$SK_START_DIR/$id.account"
    [[ -z $prompt_handoff ]] ||
      sk_abort_prompt_stage "$prompt_file" "$prompt_handoff" \
        "$SK_STAGED_PROMPT_IDENTITY" >/dev/null 2>&1 || true
    sk_lock_release 9
    sk_die "selected account launch record could not be written; no session was created"
    return 1
  fi
  if [[ -n $requested_model ]] &&
     ! sk_write_launch_request "$id" "$provider" "$cwd" "$requested_model" "$launch_key"; then
    command rm -f -- "$SK_START_DIR/$id" "$SK_START_DIR/$id.account"
    [[ -z $prompt_handoff ]] ||
      sk_abort_prompt_stage "$prompt_file" "$prompt_handoff" \
        "$SK_STAGED_PROMPT_IDENTITY" >/dev/null 2>&1 || true
    sk_lock_release 9
    sk_die "requested model launch record could not be written; no session was created"
    return 1
  fi
  if [[ -n $prompt_handoff ]] &&
     ! sk_finalize_prompt_source "$prompt_file" "$SK_STAGED_PROMPT_IDENTITY"; then
    sk_log_action prompt_handoff source-retained >/dev/null 2>&1 || true
  fi
  if [[ -n $requested_worktree ]]; then
    # The session exists either way; an unbound record only costs the picker
    # its session label, so this reports rather than unwinds a live launch.
    local -a bind_argv=(python3 "$INVENTORY_CORE" worktree bind
      --path "$cwd" --shpool-id "$id")
    [[ -z $launch_key ]] || bind_argv+=(--launch-key "$launch_key")
    if ! "${bind_argv[@]}" >/dev/null; then
      printf 'session-kit: the session runs in %s, but its worktree record was not updated\n' \
        "$cwd" >&2
    fi
    if (( auto_worktree )); then
      printf 'Its own copy of the code on branch %s. It goes back when this session closes, unless there is unmerged work in it.\n' \
        "$requested_worktree"
    else
      printf 'Running in its own worktree on branch %s.\n' "$requested_worktree"
    fi
  fi
  # The one verb that CREATES a session logged nothing at all, so "what made
  # this session, and when" had no answer anywhere on the machine. It does now,
  # before the session can exist.
  python3 "$INVENTORY_CORE" action-log session_create requested \
    --session "$id" >/dev/null 2>&1 || true
  printf 'Starting a %s session in %s\n' "$(sk_provider_name "$provider")" "$cwd"
  local creation_floor_ms
  creation_floor_ms=$(sk_now_unix_ms) || {
    sk_lock_release 9
    return 1
  }
  local allocated_id=$id
  if ! sk_attach_new_unique "$id" "$cwd" "$requested_origin"; then
    id=$SK_ATTACHED_ID
    local quarantine
    quarantine=$(sk_quarantine_start_record "$id" attach-failed 2>/dev/null || true)
    [[ -z $requested_model || -z $quarantine ]] ||
      sk_quarantine_launch_request "$id" "$quarantine" >/dev/null 2>&1 || true
    sk_lock_release 9
    sk_die "the session manager did not confirm the new session; nothing was opened. Try 'sp new' again"
    return 1
  fi
  id=$SK_ATTACHED_ID
  # sk_attach_new_unique, called above, publishes this sourced-module field.
  # shellcheck disable=SC2153
  sk_attach_status=$SK_ATTACH_STATUS
  if [[ $id != "$allocated_id" ]]; then
    [[ -z $prompt_handoff ]] || prompt_handoff=$SK_START_DIR/$id.prompt
    python3 "$INVENTORY_CORE" action-log session_create requested \
      --session "$id" >/dev/null 2>&1 || true
    if [[ -n $requested_worktree ]]; then
      local -a rebound_argv=(python3 "$INVENTORY_CORE" worktree bind
        --path "$cwd" --shpool-id "$id")
      [[ -z $launch_key ]] || rebound_argv+=(--launch-key "$launch_key")
      "${rebound_argv[@]}" >/dev/null ||
        printf 'session-kit: the new session uses %s, but its worktree record still names the stale entry %s\n' \
          "$id" "$allocated_id" >&2
    fi
  fi
  # A timed-out reply (124/137) does not prove the session was NOT created —
  # generation capture below settles it either way.
  if ! sk_capture_session_generation "$id" "$creation_floor_ms"; then
    local quarantine
    quarantine=$(sk_quarantine_start_record "$id" generation-unproven 2>/dev/null || true)
    [[ -z $requested_model || -z $quarantine ]] ||
      sk_quarantine_launch_request "$id" "$quarantine" >/dev/null 2>&1 || true
    sk_lock_release 9
    sk_die "the session may be open, but Session Kit could not confirm it. Check 'sp list' before starting another"
    return 1
  fi
  local created_started=$SK_CREATED_STARTED
  local created_boot_id=$SK_CREATED_BOOT_ID
  local created_shell_pid=$SK_CREATED_SHELL_PID created_shell_start=$SK_CREATED_SHELL_START
  local created_daemon_pid=$SK_CREATED_DAEMON_PID created_daemon_start=$SK_CREATED_DAEMON_START
  # A reusable manager name is not provenance. Stamp only after the exact
  # shell generation is known; until then the safe default leaves the row in
  # the person's list rather than binding this origin to a later namesake.
  sk_record_origin "$id" "$requested_origin" "$created_started" \
    "$created_shell_pid" "$created_shell_start"
  if [[ -n $requested_model ]] &&
     ! sk_arm_launch_request "$id" "$provider" "$cwd" "$requested_model" "$launch_key" \
       "$created_boot_id" "$created_started" "$created_shell_pid" "$created_shell_start" \
       "$created_daemon_pid" "$created_daemon_start"; then
    local quarantine
    quarantine=$(sk_quarantine_start_record "$id" model-arming-failed 2>/dev/null || true)
    [[ -z $quarantine ]] ||
      sk_quarantine_launch_request "$id" "$quarantine" >/dev/null 2>&1 || true
    sk_lock_release 9
    sk_die "the session is open, but the model you asked for could not be set. Close it from the picker and start again"
    return 1
  fi
  sk_write_generation_record "$id" "$provider" "$cwd" "$launch_uuid" \
    "$created_boot_id" "$created_started" "$created_shell_pid" "$created_shell_start" \
    "$created_daemon_pid" "$created_daemon_start" "$launch_mode" || {
      local quarantine
      quarantine=$(sk_quarantine_start_record "$id" arming-failed 2>/dev/null || true)
      [[ -z $requested_model || -z $quarantine ]] ||
        sk_quarantine_launch_request "$id" "$quarantine" >/dev/null 2>&1 || true
      sk_lock_release 9
      sk_die "the session is open, but Session Kit could not record how it started. Close it from the picker and start again"
      return 1
  }
  sk_lock_release 9
  if [[ $provider == shell ]] &&
     ! sk_clear_start_record "$id" "$provider" "$cwd" "$launch_uuid" \
       "$created_boot_id" "$created_started" "$created_shell_pid" \
       "$created_shell_start" "$created_daemon_pid" "$created_daemon_start" \
       "$launch_mode"; then
    sk_die "the session is open, but Session Kit could not clear its launch record"
    return 1
  fi
  if [[ $provider != shell ]] && ! sk_wait_for_provider "$id" "$provider" "$cwd" \
    "$launch_uuid" \
    "$created_boot_id" "$created_started" "$created_shell_pid" "$created_shell_start" \
    "$created_daemon_pid" "$created_daemon_start" "$launch_mode"; then
    sk_die "the session opened, but Session Kit could not confirm that $(sk_provider_name "$provider") started. Find it in 'sp list' and run 'sp verify-start <session>'"
    return 1
  fi
  if [[ $provider != shell && -n ${SK_PROVEN_UUID:-} ]]; then
    if [[ -n $requested_account ]] &&
       ! python3 "$INVENTORY_CORE" account bind "$provider" "$SK_PROVEN_UUID" \
         "$requested_account" --source launch >/dev/null; then
      sk_die "the session is open, but its verified account binding could not be recorded"
      return 1
    fi
    # If Claude's first-window pre-bake could not complete, reserve a color
    # against the exact live palette now that its conversation ID is known.
    # The session is still detached here, so the native propagation lands
    # before the caller can attach. Future resumes keep the stored override.
    if [[ $provider == claude && $launch_mode == new ]]; then
      python3 "$INVENTORY_CORE" color conversation-pick claude "$SK_PROVEN_UUID" \
        >/dev/null 2>&1 || true
    fi
    # Every proven provider session gets its stable color pushed once at
    # creation, so Claude shows it natively from the first resume onward. A
    # failure here is the difference between a window that matches the picker
    # and one that does not, so it is reported rather than swallowed.
    if ! python3 "$INVENTORY_CORE" color propagate "$provider" "$SK_PROVEN_UUID" \
      >/dev/null 2>&1; then
      printf 'The color could not be written for this session; it takes the kit color from its next start.\n' >&2
    fi
  fi
  if [[ ${SESSION_KIT_BACKGROUND:-0} == 1 ]]; then
    printf '%s\n' "$id"
    return
  fi
  # `sp new` used to end on a promise -- "Starting a Claude session" -- and
  # never said whether it arrived. Say what happened, by the number the picker
  # and `sp list` show, before the attach takes the screen.
  if make_snapshot >/dev/null 2>&1 &&
     sk_resolve "$SNAPSHOT" "$id" >/dev/null 2>&1 &&
     [[ -n ${SK_NUMBER:-} ]]; then
    printf 'Opened session %s.\n' "$SK_NUMBER"
  else
    printf 'Opened the session.\n'
  fi
  # attach_id execs, so the EXIT trap never runs from here. Drop the snapshot
  # now rather than leave one temp file behind per created session.
  cleanup_snapshot
  SK_PROVIDER=$provider
  SK_UUID=${SK_PROVEN_UUID:-}
  attach_id "$id"
}

restore_exact() {
  local provider=$1 uuid=$2 cwd=$3 account_alias=${4:-} requested_model=${5:-}
  sk_require_integration || return 1
  if [[ -n $requested_model ]]; then
    # A restore that names a model is the model-change path: the same
    # conversation, resumed by a provider started on the model asked for.
    # Validation is the same one `sp new --model` uses, so an identifier the
    # kit would refuse to launch can never reach a launch record.
    local validated_model
    validated_model=$(python3 "$INVENTORY_CORE" validate-worker-model \
      "$provider" "$requested_model" 2>/dev/null) || {
        sk_die "unsupported or unsafe $provider model identifier"
        return 2
      }
    [[ $validated_model == "$requested_model" ]] || {
      sk_die "model validation did not preserve the identifier"
      return 2
    }
  fi
  [[ $provider =~ ^(claude|codex)$ ]] || {
    sk_die "restore provider must be claude or codex"
    return 1
  }
  [[ $uuid =~ ^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$ ]] || {
    sk_die "restore requires a conversation UUID"
    return 1
  }
  uuid=${uuid,,}
  # A delegated session ran in a working copy that its own close gave back, so
  # the directory a restore is handed can be one the kit removed on purpose.
  # The tombstone the release leaves behind names the repository that copy came
  # from, and the conversation reopens there rather than dead-ending on a
  # directory that no longer exists.
  if [[ $cwd == /* && ! -d $cwd ]]; then
    local released_repo=
    released_repo=$(python3 "$INVENTORY_CORE" worktree lookup --path "$cwd" \
      --include-released 2>/dev/null | python3 -c '
import json,sys
try:
    record = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
repo = record.get("repo") or ""
if not record.get("released") or not isinstance(repo, str) or not repo.startswith("/"):
    raise SystemExit(1)
print(repo)
' 2>/dev/null) || released_repo=""
    if [[ -n $released_repo && -d $released_repo ]]; then
      printf 'Its own copy of the code was given back when it closed, so this reopens in %s.\n' \
        "$released_repo"
      cwd=$released_repo
    fi
  fi
  [[ $cwd == /* && -d $cwd ]] || {
    sk_die "restore cwd is not an existing absolute directory"
    return 1
  }
  cwd=$(cd -- "$cwd" && pwd -P) || return 1
  if [[ -z $account_alias ]]; then
    # A conversation bound to an account profile only exists in that
    # profile's transcript tree; restoring it on the default profile kills
    # the original and then fails identity proof. When no caller names the
    # alias, the binding ledger does — best-effort, empty when unproven.
    account_alias=$(python3 "$INVENTORY_CORE" account source "$provider" "$uuid" \
      2>/dev/null |
      python3 -c 'import json,sys
try:
    print(json.loads(sys.stdin.read()).get("alias") or "")
except Exception:
    print("")' 2>/dev/null) || account_alias=""
    [[ $account_alias =~ ^[a-z][a-z0-9_-]{0,11}$ ]] || account_alias=""
  fi
  if [[ -n $account_alias ]]; then
    [[ $account_alias =~ ^[a-z][a-z0-9_-]{0,11}$ ]] || {
      sk_die "restore account alias is invalid"
      return 1
    }
    python3 "$INVENTORY_CORE" account resume-profile "$provider" "$account_alias" \
      >/dev/null 2>&1 || {
      sk_die "restore account profile is unavailable or does not match its login"
      return 1
    }
  fi

  sk_prepare_state || return 1
  exec 9>"$SK_STATE_DIR/create.lock" || return 1
  sk_lock_acquire 9 "$SK_STATE_DIR/create.lock" || { exec 9>&-; return 1; }
  if ! sk_require_integration; then
    sk_lock_release 9
    return 1
  fi
  # Recovery uses the same guard inventory as every other mutating command.
  # A half-started session that has not yet published a provider identity is
  # unrelated to this restore and must not be able to block it; the exact
  # guarantees that matter here are the duplicate-conversation check below and
  # the exact generation proof captured after creation.
  make_guard_snapshot || {
    sk_lock_release 9
    return 1
  }
  local duplicate_check ignore_stale_id=${SK_RESTORE_IGNORE_STALE_ID:-}
  duplicate_check=$(python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
wanted_provider=sys.argv[2]
wanted_uuid=sys.argv[3].lower()
stale_id=sys.argv[4]
rows=[
    row for row in d.get("sessions",[])
    if not stale_id or row.get("shpool_id_raw") != stale_id
]
rows+=d.get("outside_agents",[])
print("yes" if any(
    isinstance(row,dict)
    and row.get("provider")==wanted_provider
    and isinstance((row.get("identity") or {}).get("uuid"),str)
    and row["identity"]["uuid"].lower()==wanted_uuid
    for row in rows
) else "no")
  ' "$SNAPSHOT" "$provider" "$uuid" "$ignore_stale_id") || {
    sk_lock_release 9
    sk_die "could not verify open conversations; nothing was restored"
    return 1
  }
  if [[ $duplicate_check == yes ]]; then
    sk_lock_release 9
    sk_die "that conversation is already open"
    return 1
  elif [[ $duplicate_check != no ]]; then
    sk_lock_release 9
    sk_die "active conversation UUID check returned an invalid result"
    return 1
  fi

  local id
  id=$(sk_allocate_id) || {
    sk_lock_release 9
    return 1
  }
  sk_write_start_record "$id" "$provider" "$cwd" "$uuid" resume || {
    sk_lock_release 9
    return 1
  }
  # A restore brings back a session that already had an origin, so the record
  # of what it was outranks any reading of who is restoring it -- in BOTH
  # directions. Reading only the machine half meant a conversation recorded as
  # the operator's, restored by anything running inside a session (a sweep, or
  # the operator typing `sp restore-exact` in one of their own windows), fell
  # through to the caller reading and came back a machine's: their own
  # conversation, gone from their own list, by their own keystroke. Repair paths
  # still declare an origin explicitly and that declaration wins over both.
  # Without any of this, the recovery sweep -- which runs from the login
  # shell, where nothing says "machine" -- brought every agent's session back
  # as one of the operator's rows.
  local restore_origin=${SESSION_KIT_ORIGIN:-}
  if [[ $restore_origin != human && $restore_origin != machine ]]; then
    restore_origin=$(sk_recorded_origin "$provider" "$uuid")
    case $restore_origin in
      human|machine) ;;
      # Everything else -- no record, a ledger that did not read, a ledger
      # that would not parse -- is UNKNOWN provenance, and unknown is the
      # person's. The caller is deliberately not consulted here. Reading the
      # caller answers the question "who is creating this?", and a restore
      # creates nothing: it brings back a conversation that already had an
      # owner. Answering the wrong question froze the caller's context into a
      # permanent machine stamp for any conversation with no record -- so a
      # repair, a model change, or `sp restore-exact` typed inside one of the
      # operator's own windows took a session out of their list and no later
      # refresh could put it back. An agent that wants its restore marked
      # says so with SESSION_KIT_ORIGIN, exactly as it does at creation.
      *) restore_origin=human ;;
    esac
  fi
  if [[ -n $account_alias ]] &&
     ! sk_write_account_record "$id" "$provider" "$account_alias"; then
    command rm -f -- "$SK_START_DIR/$id"
    sk_lock_release 9
    sk_die "restore account launch record could not be written; nothing launched"
    return 1
  fi
  if [[ -n $requested_model ]] &&
     ! sk_write_launch_request "$id" "$provider" "$cwd" "$requested_model" ""; then
    command rm -f -- "$SK_START_DIR/$id" "$SK_START_DIR/$id.account"
    sk_lock_release 9
    sk_die "requested model launch record could not be written; nothing launched"
    return 1
  fi
  local creation_floor_ms
  creation_floor_ms=$(sk_now_unix_ms) || {
    sk_lock_release 9
    return 1
  }
  # A restored conversation must come back looking like itself: same name,
  # same color, with nobody having to do anything. Both are provider-store
  # writes that only take effect at a process start, so both happen HERE,
  # before the provider is launched, and a failure is said out loud rather
  # than discovered on a nameless window.
  if ! python3 "$INVENTORY_CORE" color propagate "$provider" "$uuid" \
    >/dev/null 2>&1; then
    printf 'The color could not be written for this conversation; the window opens in its own color.\n' >&2
  fi
  local name_push_status=0
  python3 "$INVENTORY_CORE" alias push "$provider" "$uuid" >/dev/null ||
    name_push_status=$?
  case $name_push_status in
    0) ;;
    3)
      # Some surfaces took the name and some did not. On Codex that means the
      # index has it and the status bar does not, which reads as a nameless
      # window even though a push "succeeded" -- so it is said, not hidden.
      printf 'The name reached only part of this conversation; the window may open unnamed.\n' >&2
      ;;
    *)
      printf 'The name could not be written for this conversation; the window opens unnamed.\n' >&2
      ;;
  esac
  if ! sk_attach_new_unique "$id" "$cwd" "$restore_origin"; then
    id=$SK_ATTACHED_ID
    local quarantine
    quarantine=$(sk_quarantine_start_record "$id" attach-failed 2>/dev/null || true)
    sk_lock_release 9
    sk_die "the session manager did not confirm the restored session; nothing was opened. Try the restore again"
    return 1
  fi
  id=$SK_ATTACHED_ID
  sk_attach_status=$SK_ATTACH_STATUS
  if ! sk_capture_session_generation "$id" "$creation_floor_ms"; then
    local quarantine
    quarantine=$(sk_quarantine_start_record "$id" generation-unproven 2>/dev/null || true)
    sk_lock_release 9
    sk_die "the restored session may be open, but Session Kit could not confirm it. Check 'sp list' before restoring again"
    return 1
  fi
  sk_record_origin "$id" "$restore_origin" "$SK_CREATED_STARTED" \
    "$SK_CREATED_SHELL_PID" "$SK_CREATED_SHELL_START"
  if [[ -n $requested_model ]] &&
     ! sk_arm_launch_request "$id" "$provider" "$cwd" "$requested_model" "" \
       "$SK_CREATED_BOOT_ID" "$SK_CREATED_STARTED" "$SK_CREATED_SHELL_PID" \
       "$SK_CREATED_SHELL_START" "$SK_CREATED_DAEMON_PID" "$SK_CREATED_DAEMON_START"; then
    local quarantine
    quarantine=$(sk_quarantine_start_record "$id" model-arming-failed 2>/dev/null || true)
    [[ -z $quarantine ]] ||
      sk_quarantine_launch_request "$id" "$quarantine" >/dev/null 2>&1 || true
    sk_lock_release 9
    sk_die "the session is open, but the model you asked for could not be set. Close it from the picker and restore again"
    return 1
  fi
  sk_write_generation_record "$id" "$provider" "$cwd" "$uuid" \
    "$SK_CREATED_BOOT_ID" "$SK_CREATED_STARTED" "$SK_CREATED_SHELL_PID" "$SK_CREATED_SHELL_START" \
    "$SK_CREATED_DAEMON_PID" "$SK_CREATED_DAEMON_START" resume || {
      local quarantine
      quarantine=$(sk_quarantine_start_record "$id" arming-failed 2>/dev/null || true)
      sk_lock_release 9
      sk_die "the restored session is open, but Session Kit could not record how it started. Close it from the picker and restore again"
      return 1
    }
  local created_started=$SK_CREATED_STARTED
  local created_boot_id=$SK_CREATED_BOOT_ID
  local created_shell_pid=$SK_CREATED_SHELL_PID created_shell_start=$SK_CREATED_SHELL_START
  local created_daemon_pid=$SK_CREATED_DAEMON_PID created_daemon_start=$SK_CREATED_DAEMON_START
  sk_lock_release 9
  if ! sk_wait_for_provider "$id" "$provider" "$cwd" "$uuid" \
    "$created_boot_id" "$created_started" "$created_shell_pid" "$created_shell_start" \
    "$created_daemon_pid" "$created_daemon_start" resume; then
    sk_die "the session opened, but Session Kit could not confirm that $(sk_provider_name "$provider") reopened the conversation. Find it in 'sp list' and run 'sp verify-start <session>'"
    return 1
  fi
  if [[ -n $account_alias ]] &&
     ! python3 "$INVENTORY_CORE" account bind "$provider" "$uuid" "$account_alias" \
       --source restore >/dev/null; then
    sk_die "the conversation was restored, but its account binding could not be recorded"
    return 1
  fi
  # Keep the closed evidence. The shared recovery projection hides it while
  # this exact conversation is live, then can offer it again if the restored
  # provider disappears before another loss snapshot records the transition.
  printf '%s\n' "$id"
}
