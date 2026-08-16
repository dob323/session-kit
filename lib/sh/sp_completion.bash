#!/usr/bin/env bash
# session-kit managed bash completion v1
#
# Tab completion for the Session Kit commands that take arguments: sp,
# session-kit, shpool_status, shpool_reaper, and codex_resume_here. Source this
# file; do not execute it. The installer puts a copy under each of those names
# in the user's bash-completion directory, so bash-completion loads the right
# one on demand; without that package, sourcing any single copy registers all
# five.
#
# Two rules this file keeps:
#
#   1. It never runs a Session Kit command. A completion that shelled out to
#      `sp list` or `shpool_status` would build a fresh inventory -- which
#      writes state and can take a second -- on every Tab press, from inside
#      the user's prompt. That is why terminal numbers are not completed here:
#      the only honest source for them is a live snapshot, and reading one on
#      Tab is a state change nobody asked for.
#   2. It stays inside Bash 3.2. macOS logins still land in the system Bash,
#      and a completion file that fails to parse there breaks the prompt of a
#      shell that never even ran Session Kit. So: no mapfile, no associative
#      arrays, no ${var,,}.
#
# The only file it reads is the projects list, a small owner-private TSV, so
# `sp new <alias>` completes the aliases the user actually configured.
#
# shellcheck shell=bash

_session_kit_words() {
  local candidates=$1 current=${2:-}
  local IFS=$' \t\n'
  # Word splitting is the point: COMPREPLY is an array of candidates, and
  # mapfile -- the suggested alternative -- is Bash 4 only.
  # shellcheck disable=SC2207
  COMPREPLY=($(compgen -W "$candidates" -- "$current"))
}

_session_kit_project_aliases() {
  local file=${SESSION_KIT_PROJECTS_FILE:-}
  if [ -z "$file" ]; then
    file=${XDG_CONFIG_HOME:-$HOME/.config}/session-kit/projects.tsv
  fi
  [ -r "$file" ] || return 0
  # Field 1 of every non-comment line, bounded so a corrupted or enormous file
  # cannot stall the prompt.
  awk -F'\t' '
    /^[[:space:]]*($|#)/ { next }
    $1 ~ /^[A-Za-z0-9._-]+$/ { print $1 }
    NR > 500 { exit }
  ' "$file" 2>/dev/null | tr '\n' ' '
}

_sp() {
  local current previous verb
  current=${COMP_WORDS[COMP_CWORD]}
  previous=${COMP_WORDS[COMP_CWORD-1]}
  verb=${COMP_WORDS[1]}
  COMPREPLY=()

  # The verbs a person types. picker-* and restore-exact are machine verbs
  # called by the picker and the managed shell with proof arguments; they are
  # left out here for the same reason `sp help` leaves them out.
  local verbs="list new go takeover close teardown detail health prune repair
    verify-start name self-name color account worktree find history
    recover help"

  if [ "$COMP_CWORD" -eq 1 ]; then
    _session_kit_words "$verbs" "$current"
    return 0
  fi

  case "$verb" in
    help)
      [ "$COMP_CWORD" -eq 2 ] || return 0
      _session_kit_words "sessions names accounts history selectors
        unavailable exit-codes completion machine" "$current"
      ;;
    worktree)
      [ "$COMP_CWORD" -eq 2 ] || return 0
      _session_kit_words "list materialize bind lookup teardown" "$current"
      ;;
    new)
      case "$previous" in
        --account | --model | --launch-key)
          return 0
          ;;
        --origin)
          _session_kit_words "human machine" "$current"
          return 0
          ;;
        --prompt-file)
          return 0
          ;;
      esac
      case "$current" in
        -*)
          _session_kit_words "--account --model --launch-key --origin --prompt-file --worktree" \
            "$current"
          return 0
          ;;
      esac
      if [ "$COMP_CWORD" -eq 2 ]; then
        _session_kit_words "claude codex shell $(_session_kit_project_aliases)" \
          "$current"
      elif [ "$COMP_CWORD" -eq 3 ]; then
        _session_kit_words "$(_session_kit_project_aliases)" "$current"
      else
        _session_kit_words "--account --model --launch-key --origin --prompt-file --worktree" \
          "$current"
      fi
      ;;
    name)
      [ "$COMP_CWORD" -eq 2 ] || return 0
      _session_kit_words "reset" "$current"
      ;;
    color)
      if [ "$COMP_CWORD" -eq 2 ]; then
        _session_kit_words "reset reconcile" "$current"
      elif [ "$COMP_CWORD" -eq 3 ] && [ "$previous" != reset ]; then
        # Both palettes: a session's provider decides which half is valid, and
        # this file deliberately does not ask the manager which one it is.
        _session_kit_words "red blue green yellow purple orange pink cyan
          lime magenta silver sand sky sea" "$current"
      fi
      ;;
    account)
      if [ "$COMP_CWORD" -eq 2 ]; then
        _session_kit_words "list adopt-default enroll verify configure-feeds" \
          "$current"
      elif [ "$COMP_CWORD" -eq 3 ]; then
        case "${COMP_WORDS[2]}" in
          list | adopt-default | enroll | verify)
            _session_kit_words "claude codex" "$current"
            ;;
        esac
      fi
      ;;
  esac
  return 0
}

_session_kit() {
  local current previous verb
  current=${COMP_WORDS[COMP_CWORD]}
  previous=${COMP_WORDS[COMP_CWORD-1]}
  verb=${COMP_WORDS[1]}
  COMPREPLY=()

  if [ "$COMP_CWORD" -eq 1 ]; then
    _session_kit_words "doctor projects update rollback services enable-login
      disable-login uninstall help" "$current"
    return 0
  fi

  case "$verb" in
    doctor)
      _session_kit_words "--json" "$current"
      ;;
    projects)
      [ "$COMP_CWORD" -eq 2 ] || return 0
      _session_kit_words "discover import list add ignore unignore" "$current"
      ;;
    services)
      [ "$COMP_CWORD" -eq 2 ] || return 0
      _session_kit_words "enable disable status" "$current"
      ;;
    update)
      case "$previous" in
        --source)
          _session_kit_filedir
          return 0
          ;;
        --journal)
          _session_kit_words "on off" "$current"
          return 0
          ;;
      esac
      _session_kit_words "--source --enable-login --disable-login --journal" \
        "$current"
      ;;
    rollback)
      [ "$previous" = --to ] && return 0
      _session_kit_words "--to" "$current"
      ;;
    uninstall)
      _session_kit_words "--purge-code --purge-config" "$current"
      ;;
  esac
  return 0
}

_shpool_status() {
  local current
  current=${COMP_WORDS[COMP_CWORD]}
  COMPREPLY=()
  [ "$COMP_CWORD" -eq 1 ] || return 0
  _session_kit_words "--rows --json --strict-json --guard-json --waiting-count
    --detail --lookup --render-file --lookup-file --recovery-pending-list
    --recovery-pending-ack --help" "$current"
}

_shpool_reaper() {
  local current
  current=${COMP_WORDS[COMP_CWORD]}
  COMPREPLY=()
  [ "$COMP_CWORD" -eq 1 ] || return 0
  _session_kit_words "--candidates --auto-close --verify-candidate --help" \
    "$current"
}

_codex_resume_here() {
  local current previous
  current=${COMP_WORDS[COMP_CWORD]}
  previous=${COMP_WORDS[COMP_CWORD-1]}
  COMPREPLY=()
  if [ "$previous" = --cwd ]; then
    _session_kit_dirs
    return 0
  fi
  case "$current" in
    -*) _session_kit_words "--print --cwd --help" "$current" ;;
  esac
  return 0
}

# bash-completion's _filedir is not guaranteed to be loaded, so both of these
# fall back to compgen's own file and directory generators.
_session_kit_filedir() {
  local current=${COMP_WORDS[COMP_CWORD]}
  if declare -F _filedir >/dev/null 2>&1; then
    _filedir
  else
    # shellcheck disable=SC2207  # see _session_kit_words
    COMPREPLY=($(compgen -f -- "$current"))
  fi
}

_session_kit_dirs() {
  local current=${COMP_WORDS[COMP_CWORD]}
  if declare -F _filedir >/dev/null 2>&1; then
    _filedir -d
  else
    # shellcheck disable=SC2207  # see _session_kit_words
    COMPREPLY=($(compgen -d -- "$current"))
  fi
}

complete -F _sp sp
complete -F _session_kit session-kit
complete -F _shpool_status shpool_status
complete -F _shpool_reaper shpool_reaper
complete -F _codex_resume_here codex_resume_here
