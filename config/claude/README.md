# Claude Code integration files (canonical copies)

These two files make a Session Kit name visible inside a Claude Code window.
They are hand-deployed: no install, update, or rollback copies them, and no
uninstall removes them. The copies here are canonical, so an edit to a live file
has to be mirrored back or the next person reads a stale source.

| repo copy | live file |
|---|---|
| `nameintent_title.sh` | `~/.claude/hooks/nameintent_title.sh` |
| `statusline.sh` | `~/.claude/statusline.sh` |

`session-kit doctor` checks the hook and reports it as `naming-hook`: the file
must be an owner-only executable regular file, it must be registered in
`~/.claude/settings.json` for all three events below, and it must produce the
expected title JSON when doctor runs it against a throwaway fixture `HOME`. A
missing piece is a warning, not a failure. Doctor does not look at
`statusline.sh` at all; that file's only automated coverage is a repository test
asserting its color table still matches the Claude palette.

## How a session name reaches a Claude window

The kit pushes a name onto three surfaces: `~/.claude/sessions/<uuid>.nameintent`,
an `agent-name` record appended to the matching transcripts, and the per-PID
`~/.claude/sessions/<pid>.json` record whose `sessionId` matches the
conversation. The three feed different parts of the interface, and only the
first is read by the hook.

Claude Code applies an external `sessionTitle` from hook output at exactly two
moments — probed on 2.1.221 (2026-08-04): **SessionStart** and
**UserPromptSubmit**. `Stop` and `PostToolUse` output is ignored. A window that
is only being watched therefore keeps its old tab title and top-right chip until
the human sends the next prompt; the name is not lost, it is queued behind an
event that has not happened yet.

The status line is the one surface that repaints live mid-turn. It renders the
nameintent name at the start of line 1, tinted with the session's kit color.
Register it with a refresh timer so idle windows update too:

```json
"statusLine": {
  "type": "command",
  "command": "~/.claude/statusline.sh",
  "refreshInterval": 2
}
```

Three things about that script are worth knowing before deploying it. It reads
`display_color` out of `~/.local/state/session-kit/inventory.json` by a literal
path, so an installation that moves its state root with `SESSION_KIT_STATE_DIR`
or `XDG_STATE_HOME` gets the untinted fallback rather than an error. Its color
table covers exactly the eight names in the Claude palette and nothing else,
which is complete rather than partial: the row is looked up by Claude Code's own
`session_id`, so only a Claude color can ever reach it, and the six Codex-only
names cannot. And it declares itself the Linux status line — the quota segment
on line 2 uses GNU `stat -c` and reads `~/.claude/cache/quota_headers`, a cache
refreshed by `~/.claude/statusline-quota-refresh.sh` that this repository does
not ship. Without that script the segment renders `quota --` and everything else
still works.

Windows that boot before their name exists are restarted-as-resumed once at
reopen by the provider bounce (`picker_bounce_claude` in
`lib/sh/sp_provider_bounce.sh`, sourced by `bin/sp`), which boots them through
SessionStart with the name already applied. The bounce is heavily guarded, since
restarting a provider that is mid-thought would lose work: it runs only against
a detached ready session with no subagents, whose provider is idle or awaiting a
reply, that has produced no output for at least 120 seconds, and whose PID and
process start time still match the frozen proof. It also asks the kit whether a
real prompt already followed the name intent — if one did, the live window has
the name, so the pending marker is dropped and nothing restarts.

## Hook registration

`nameintent_title.sh` must be registered in `~/.claude/settings.json` for
`SessionStart`, `UserPromptSubmit`, and `Stop`. The first two are the events
that can retitle a live window. `Stop` cannot, and the hook emits nothing there;
it is registered because Stop is where the kit derives an automatic title for a
resume-launched conversation that Claude never auto-titled, writing the intent
file that renames the window at the next prompt.

The hook fails open. Any unexpected input, missing file, or failed lookup exits
0 with no output, which leaves the window titled as it was.
