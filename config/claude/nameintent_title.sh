#!/usr/bin/env bash
# User-level hook (SessionStart + UserPromptSubmit): if this session has a
# recorded name intent (~/.claude/sessions/<sessionId>.nameintent — written by
# session-kit name propagation or agent_name_session.sh), set the live TUI
# title to it. Project-agnostic so kit-assigned names rename Claude Code in
# every project, not just repos that ship their own coordination hooks.
# FAIL OPEN: any problem exits 0 with no output.

set -uo pipefail
INPUT=$(cat 2>/dev/null) || exit 0
SID=$(printf '%s' "$INPUT" | jq -r '.session_id // ""' 2>/dev/null)
EVENT=$(printf '%s' "$INPUT" | jq -r '.hook_event_name // ""' 2>/dev/null)
case "$EVENT" in SessionStart|UserPromptSubmit|Stop) ;; *) exit 0 ;; esac
[ -n "$SID" ] || exit 0
INTENT="$HOME/.claude/sessions/$SID.nameintent"
if [ -f "$INTENT" ] && [ ! -L "$INTENT" ]; then
  # Stop stays silent: the runtime ignores sessionTitle there (retested
  # on 2.1.221, as did PostToolUse — only SessionStart and UserPromptSubmit
  # apply it; probes 2026-08-04).
  [ "$EVENT" = Stop ] && exit 0
  TITLE="$(tr -d '\n\r' < "$INTENT" 2>/dev/null | cut -c1-64)"
  [ -n "$TITLE" ] || exit 0
  jq -nc --arg e "$EVENT" --arg t "$TITLE" \
    '{hookSpecificOutput:{hookEventName:$e,sessionTitle:$t}}' 2>/dev/null
  exit 0
fi

# No name intent: pre-baked (resume-launched) conversations never receive
# Claude's own auto-title, so the session kit derives one from the first real
# prompt. Cheap grep pre-filter keeps ordinary sessions on a no-Python path;
# the kit verb re-checks eligibility strictly and fails open.
case "$EVENT" in UserPromptSubmit|Stop) ;; *) exit 0 ;; esac
INV="$HOME/.local/lib/session-kit/current/lib/session_inventory.py"
[ -f "$INV" ] || exit 0
TP=$(printf '%s' "$INPUT" | jq -r '.transcript_path // ""' 2>/dev/null)
[ -n "$TP" ] && [ -f "$TP" ] || exit 0
grep -qF '"type":"ai-title"' "$TP" 2>/dev/null && exit 0
grep -qF '"type":"agent-name"' "$TP" 2>/dev/null && exit 0
# Claude can invoke Stop before its latest transcript writes are visible. Only
# wait for a resume-launched, colored candidate; all ordinary sessions retain
# the immediate no-op path. The Python verb performs the strict final checks.
if ! grep -qF '"Continue from where you left off."' "$TP" 2>/dev/null; then
  [ "$EVENT" = Stop ] || exit 0
  grep -qF '"type":"agent-color"' "$TP" 2>/dev/null || exit 0
  for _ in {1..20}; do
    sleep 0.1
    grep -qF '"Continue from where you left off."' "$TP" 2>/dev/null && break
  done
  grep -qF '"Continue from where you left off."' "$TP" 2>/dev/null || exit 0
fi
TITLE=$(printf '%s' "$INPUT" |
  timeout 10 python3 "$INV" automatic-title from-hook 2>/dev/null |
  jq -r '.title // ""' 2>/dev/null)
[ -n "$TITLE" ] || exit 0
# Stop output cannot retitle the live window; the intent file written by the
# verb renames it at the next prompt, and records cover picker/chip surfaces.
[ "$EVENT" = UserPromptSubmit ] || exit 0
jq -nc --arg e "$EVENT" --arg t "$TITLE" \
  '{hookSpecificOutput:{hookEventName:$e,sessionTitle:$t}}' 2>/dev/null
exit 0
