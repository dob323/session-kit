# Claude Code integration files (canonical copies)

Live locations (hand-deployed, verified by `session-kit doctor`, NOT
installed by release rollovers — edits there must be mirrored here):

| repo copy | live file |
|---|---|
| `nameintent_title.sh` | `~/.claude/hooks/nameintent_title.sh` |
| `statusline.sh` | `~/.claude/statusline.sh` |

## How a session name reaches a Claude window

The kit pushes a name into `~/.claude/sessions/<uuid>.nameintent` (plus the
transcript `agent-name` record and the per-PID session record). Claude Code
applies an external `sessionTitle` from hook output at exactly two moments —
probed on 2.1.221 (2026-08-04): **SessionStart** and **UserPromptSubmit**.
`Stop` and `PostToolUse` output is ignored, so the tab title and the
top-right chip cannot change while the human only watches.

The status line is the one surface that repaints live mid-turn. It renders
the nameintent name at the start of line 1, tinted with the session's kit
color (`display_color` from the kit inventory, same calibrated palette as
the picker rows). Register it with a refresh timer so idle windows update
too:

```json
"statusLine": {
  "type": "command",
  "command": "~/.claude/statusline.sh",
  "refreshInterval": 2
}
```

Windows that boot before their name exists are restarted-as-resumed once at
reopen by the provider bounce (`picker_bounce_claude` in `bin/sp`), which
boots them through SessionStart with the name applied.

## Hook registration

`nameintent_title.sh` must be registered in `~/.claude/settings.json` for
`SessionStart`, `UserPromptSubmit`, and `Stop` (Stop drives only the
kit-side auto-title derivation, never a live retitle).
