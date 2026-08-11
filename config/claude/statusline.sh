#!/usr/bin/env bash
# Claude Code status line (Linux)
# Format: line 1: [MODEL] user@host:/CWD-BASENAME | PCT% context
#         line 2: 5h PCT% (left) · 7d PCT% (left) · account email (~/.claude.json oauthAccount)
# Quota segment reads ~/.claude/cache/quota_headers (refreshed in background
# by statusline-quota-refresh.sh, TTL 180s); time-left counts down to window reset.
# Session name leads line 1 (maintainer decision, 2026-08-04): the tab title
# and top-right chip only update at session start or the next prompt, so
# this is the one surface that shows a mid-turn name change while the human
# just watches.
# Source: ~/.claude/sessions/<session_id>.nameintent (kit name push).

input=$(cat)

IFS=$'\t' read -r model cwd used_pct sid < <(
  printf '%s' "$input" | jq -r '[
    .model.display_name,
    .workspace.current_dir,
    (.context_window.used_percentage | tostring),
    (.session_id // "")
  ] | @tsv'
)

session_name=""
if [ -n "$sid" ]; then
  intent="$HOME/.claude/sessions/$sid.nameintent"
  if [ -f "$intent" ] && [ ! -L "$intent" ]; then
    session_name=$(tr -d '\n\r\000-\037' < "$intent" 2>/dev/null | cut -c1-64)
  fi
fi
if [ -n "$session_name" ]; then
  # Tint with the session's kit color (inventory display_color), using the
  # kit's calibrated palette so this matches the picker rows and the chip.
  name_color=$(jq -r --arg u "$sid"     '.sessions[]? | select(.identity.uuid == $u) | .display_color // ""'     "$HOME/.local/state/session-kit/inventory.json" 2>/dev/null | head -1)
  case "$name_color" in
    red)    tint=$'\033[38;2;237;93;93m' ;;
    blue)   tint=$'\033[38;2;97;166;240m' ;;
    green)  tint=$'\033[38;2;63;221;115m' ;;
    yellow) tint=$'\033[38;2;249;215;108m' ;;
    purple) tint=$'\033[38;2;173;115;239m' ;;
    orange) tint=$'\033[38;2;242;144;81m' ;;
    pink)   tint=$'\033[38;2;240;113;177m' ;;
    cyan)   tint=$'\033[38;2;64;216;209m' ;;
    *)      tint=$'\033[1m' ;;
  esac
  printf '%s%s\033[0m — ' "$tint" "$session_name"
fi

# show only the last path component: /srv/example/app -> /app
display_cwd="/${cwd##*/}"
[ "$cwd" = "/" ] && display_cwd="/"
host=$(hostname -s)
reset=$'\033[0m'

if [ "$used_pct" = "null" ] || [ -z "$used_pct" ]; then
  printf '[%s] %s@%s:%s | --%% context' "$model" "$USER" "$host" "$display_cwd"
else
  pct_int=$(printf '%.0f' "$used_pct")
  if [ "$pct_int" -lt 50 ]; then
    color=$'\033[38;2;80;200;120m'
  elif [ "$pct_int" -lt 80 ]; then
    color=$'\033[38;2;230;180;50m'
  else
    color=$'\033[38;2;220;60;60m'
  fi
  printf '[%s] %s@%s:%s | %s%d%%%s context' \
    "$model" "$USER" "$host" "$display_cwd" "$color" "$pct_int" "$reset"
fi

# --- Plan quota segment (5h + 7d unified windows) ---
QCACHE="$HOME/.claude/cache/quota_headers"
QTTL=180
now=$(date +%s)
acct=$(jq -r '.oauthAccount.emailAddress // empty' "$HOME/.claude.json" 2>/dev/null)
qacct=$(awk -F': ' '$1=="x-probe-account"{print $2}' "$QCACHE" 2>/dev/null)
qmtime=$(stat -c %Y "$QCACHE" 2>/dev/null || echo 0)
if [ $((now - qmtime)) -ge $QTTL ] || [ "$qacct" != "$acct" ]; then
  ( "$HOME/.claude/statusline-quota-refresh.sh" >/dev/null 2>&1 & )
fi

quota_color() {
  # args: pct warn crit
  if [ "$1" -ge "$3" ]; then printf '\033[38;2;220;60;60m'
  elif [ "$1" -ge "$2" ]; then printf '\033[38;2;230;180;50m'
  else printf '\033[38;2;80;200;120m'; fi
}

fmt_left() {
  local s=$1
  [ "$s" -lt 0 ] && s=0
  if [ "$s" -ge 86400 ]; then printf '%dd%dh' $((s/86400)) $((s%86400/3600))
  else printf '%dh%02dm' $((s/3600)) $((s%3600/60)); fi
}

if [ -f "$QCACHE" ] && [ "$qacct" = "$acct" ]; then
  u5=$(awk -F': ' 'tolower($1)=="anthropic-ratelimit-unified-5h-utilization"{print $2}' "$QCACHE")
  r5=$(awk -F': ' 'tolower($1)=="anthropic-ratelimit-unified-5h-reset"{print $2}' "$QCACHE")
  u7=$(awk -F': ' 'tolower($1)=="anthropic-ratelimit-unified-7d-utilization"{print $2}' "$QCACHE")
  r7=$(awk -F': ' 'tolower($1)=="anthropic-ratelimit-unified-7d-reset"{print $2}' "$QCACHE")
  if [ -n "$u5" ] && [ -n "$r5" ]; then
    p5=$(printf '%.0f' "$(awk "BEGIN{print $u5*100}")")
    p7=$(printf '%.0f' "$(awk "BEGIN{print ${u7:-0}*100}")")
    c5=$(quota_color "$p5" 70 90)   # pause batches at 90% of 5h
    c7=$(quota_color "$p7" 50 70)   # weekly backstop is 70%
    printf '\n5h %s%d%%%s %s · 7d %s%d%%%s %s' \
      "$c5" "$p5" "$reset" "$(fmt_left $((r5 - now)))" \
      "$c7" "$p7" "$reset" "$(fmt_left $((${r7:-$now} - now)))"
  else
    printf '\nquota --'
  fi
else
  printf '\nquota --'
fi
[ -n "$acct" ] && printf ' · %s' "$acct"
