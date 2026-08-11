# Changelog

Session Kit follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.3.0] - 2026-08-11

### Fixed

- An intake notice that fails to deliver is retried instead of abandoned.
  `flush()` now sweeps every undelivered arrival notice and amendment under its
  own relay key with per-notice backoff (1m/5m/15m/1h/6h, last rung repeating),
  `supervisor ensure` and prompt wakes trigger the sweep, and nothing is ever
  written off. `msg intake pending` reports what is owed — count, due now,
  deferred, oldest wait — and `msg intake open` carries the same block, so the
  read the supervisor already makes every turn now says what never reached it.
  Before this, delivery ran once from the recording hook and a lost send was
  marked and left, with no command able to say so.

- The supervisor's MCP definition survives a release activation. The pin is
  written through the install root's `current` pointer when the release is the
  current one (proven live over a real stdio handshake), `supervisor ensure`
  rewrites the definition on every wake so an already-drifted install repairs
  itself, and a new `supervisor mcp-config` verb regenerates it without
  creating, resuming, or messaging anything. This was the third recurrence of
  the drift; the pin now moves with activations instead of breaking on them.

- A delegated worker now receives its assignment. Workers were started with no
  prompt (Claude) or a deliberate no-work bootstrap (Codex), and nothing ever
  sent the task afterwards — delegation ended at a verified but idle worker.
  Every plan row now carries `task_text`, `acceptance_criteria`, and
  `deliverable`; `delegate()` gained a `commissioned` state that delivers the
  duty to the worker's proven identity, reserved on disk under the send key
  and settled from the receipt, so a duty that does not land is named in
  `undelivered` and finished by the next delegate under the same key rather
  than lost. Reports come back as owner-private disk receipts written before
  the intake entry, `sp msg intake duties` flags silent, owed, and unfolded
  work, and over-long duties are refused at plan time because the messaging
  core silently truncates. The two-provider/two-model composition rule is
  replaced by declared expertise: a preflight names `required_expertise_tags`
  and the only rule is that the plan covers every declared tag.

### Added

- Account selection reads real quota evidence, labelled by how it knows.
  Three readers feed the proposal: Codex rate limits parsed from the
  provider's own rollouts (`measured`), Claude per-hour load rates and refusal
  text from transcripts (`observed`, since Claude publishes no allowance
  locally), and the account roster's `u5h`/`u7d` fields for both providers
  (`feed`, yielding nothing when no roster is configured). Refusals are
  parsed for what they actually say: a weekly refusal no longer expires as a
  5-hour one, a model-scoped limit benches that model rather than the whole
  account, and a refusal already recovered from (a billed turn since) stops
  counting. Along the way two live defects in account advice fell: advice was
  consulted only for Claude, so a healthy Codex account could never be
  recommended, and the feed's explanation was read under the wrong key, so
  every recommendation arrived with a blank reason.

- A project is one thing. A canonical root directory is the single identity a
  shortcut names, a committed `session-kit.toml` describes, an intake arrives
  from inside, and a session runs in; one membership rule (deepest root at or
  above the working directory) is implemented once and shared by resolution,
  grouping, and `sp new`. The manifest travels with a clone and can carry
  provider, account, model, a startup command, and a `[[team]]` worker plan —
  but its launch fields apply only for a project this host has deliberately
  listed, `root` cannot escape the manifest's own directory, account and model
  only select among what the host already has through the existing validation,
  and startup commands are approval-gated by content digest, withdrawn on any
  edit, and surfaced rather than executed. `session-kit projects
  resolve|list|launch-plan|approve-startup|context|group-sessions|check` is
  the machine surface, and `launch-plan` names the source of every applied
  value so a session that differs from what was typed is always explainable.
  One strict TOML-subset reader runs identically on every supported Python, so
  a manifest cannot mean different things on different machines.

- Every command answers `--help`, and answers it before requiring shpool.
  `sp help` is a grouped reference covering every verb the dispatcher accepts
  (a test parses the dispatcher to keep it that way), nine `sp help <topic>`
  pages cover sessions, names, messages, accounts, history, selectors,
  exit codes, completion, and the machine verbs, and the exit-code table is
  documented once and scanned for drift. `-h`/`--help` prints to stdout and
  exits 0 across all seven entry points; an argument mistake prints a
  five-line synopsis to stderr and exits 2. Bash tab completion ships as
  `lib/sh/sp_completion.bash` (Bash 3.2-safe, never shells out, installed and
  removed with the release), and `shpool_status --help` now separates
  read-only modes from the ones that refresh caches, fill titles, or change
  recovery state — matched against the source by a test.

- `tools/install-matrix` proves the documented Linux install in clean-room
  containers: ubuntu 22.04 and 24.04, debian 12, and fedora 41 each clone the
  repository, follow `docs/install.md` as written, and must finish with
  services genuinely live and `session-kit doctor` reporting no failures. The
  tool runs from a plain checkout, a worktree, or a post-merge tree without
  dirtying any of them.

- Delegated work runs isolated and leaves a receipt. `sp new --worktree
  <branch>` materialises the branch as a git worktree under the kit's own
  state directory and starts the session there — idempotent per repository
  and branch, refused before anything moves when the branch is checked out
  elsewhere or the project is not a repository — and delegated workers get it
  by default. Rows name the branch in the picker, `sp list`, and `sp detail`;
  `sp teardown` closes the worker and prunes only a merged, clean worktree,
  never deleting the branch. Every run writes a receipt: cap snapshot,
  reported spend with the source that claimed each sample, verifier evidence,
  changed files including committed work, isolation mode, stop reason, and a
  SHA-256 integrity digest that turns outside edits into `tamper_detected`.
  A hard cap closes the run as `cap_breached` and the delegate launcher's
  gate refuses further workers; closing as `completed` requires a passing
  verifier or an explicit `--allow-unverified` written onto the record. Duty
  receipts carry the same `launch_key` as run receipts, so the two families
  join directly. A forked test child that cannot exec now exits instead of
  resuming the suite, and test sandboxes live outside the repository.

- The picker answers without attaching. `i<n>` peeks at what a session asked,
  how long it has waited, and the tail of its thread — read-only, nothing
  marked seen — and a reply typed there rides the ordinary `sp msg` delivery
  with its ledger and receipts. `/text` filters the list as you type and a
  preview belongs only to the line being typed: erase it or turn it into
  another command and the full list is back before that command runs. `g`
  jumps to the next session waiting on you, `group` buckets by state,
  provider, or project, `c` compacts rows, and one table now feeds the `?`
  screen, the footer, and the docs — with a test that parses the picker's own
  key dispatch so an undocumented key fails. An opt-in attention notifier
  rides the existing watchdog plumbing: off by default, never critical,
  nothing under a ten-minute wait, one alert per unbroken wait, with
  `extras/notify-desktop` as a working example. The default view is
  byte-identical to before.

- A stock Ubuntu machine installs. Ubuntu's per-user-group scheme leaves the
  provider homes group-writable (775), and the installer refused any
  group-writable ancestor with a raw traceback partway through the install.
  The safety check now accepts a directory whose group is provably the
  caller's own single-member private group — owner matches, gid is the
  primary gid, the group carries the account's name, lists no members, and
  no other account holds it; any lookup failure still refuses — applied
  identically at the hook path, the Codex home, and transaction recovery
  (without which an interrupted install on such a machine could never be
  finished). Refusals now name the offending directory and the exact chmod,
  `install.sh --check` reports the condition before anything is written, and
  `--non-interactive` appears in the installer's help.

- `shpool attach` keeps the shell's exit status. An upstream race poisoned
  the client's result slot with exit 1 whenever stdin closed early — visible
  as a rare test flake, real in the field for any caller that closes stdin —
  fixed by waiting the existing detach window for the status frame before
  defaulting. Patch 0001's heartbeat ack channel also no longer parks the
  shell-to-client thread on an abandoned, never-drained ack. Fifty full
  attach-suite runs pass on both the patched and pristine-plus-fix trees
  (previously about one run in eight failed), and the recorded binary
  checksum now names the toolchain that produced it.

- The public repository's CI is self-contained. The legacy-generation install
  fixture no longer pins a commit only the private history holds, the
  watchdog fixture owns its own boot clock instead of reading the host's
  uptime (which went non-positive on fresh runners and silently passed every
  repair proof on nothing), a quarantine test uses the shpool stand-in seam
  instead of requiring a real binary, and the history scan carries a
  fail-closed baseline of 37 grandfathered pre-cleanup blobs — excused by
  exact id for the private-marker rule only, never for the tree, with the
  summary reporting matched-of-listed so a stale entry shows instead of
  lasting forever.

### Security

- Source authority sees every transcript this machine holds, and says how it
  knows. Claude evidence roots now cover `$CLAUDE_CONFIG_DIR` and every kit
  account profile, with a bounded Codex-shaped discovery walk (unique-or-
  refuse, symlink-skipping) behind them; each verified event carries an
  additive authority tier naming the strength of its evidence, and
  `session-kit doctor --authority` reports the ladder read-only — including
  which sessions have a transcript their events never recorded. An
  `authority_for_intake()` policy surface exists with a fail-closed policy
  file but is wired to nothing: delegation still gates on no part of this,
  and a test fails the moment that changes without a decision. Harness
  machine envelopes can no longer become intake amendments at capture time.

- Source authority now verifies Codex sessions whose first record is the
  CLI's synthetic preamble, refuses text that begins with any variant of the
  fleet watcher's machine banner by its shared stem, and screens
  `SESSION_KIT_MACHINE_ORIGIN` at capture so a machine-originated wake can
  never become authority-capable — ANDed into capability, stored only when
  declared, and refused again at verify time. Live effect on this estate: 33
  of 38 stored Codex events verify end to end, up from zero before this
  release series; an unnamed future preamble kind still fails closed.

## [0.2.1] - 2026-08-11


### Fixed

- `sp msg intake delegate` now launches a worker. The CLI read the intake
  entry's `cwd`, but the spool has stored the project directory as
  `source_cwd` since the schema's first commit, so the launcher was always
  handed nowhere to run and every delegation died with `dispatch is
  uncertain; reconcile before retry`. The verb had therefore never worked, in
  any configuration, since it was written. The read now names the stored key,
  and `also_delivered_as` aliases are resolved before the entry is read
  because `delegate` itself accepts an alias id. A new end-to-end test drives
  the real record → preflight → delegate chain through the installed CLI with
  nothing stubbed between the stored entry and the launcher; it fails three
  times out of three against the old code.

- A managed terminal survives a clean provider exit. A clean `/exit` returned
  zero and the shell closed the shpool terminal immediately, which destroyed
  the only context that still knew the exact conversation identity a reopen
  needs; a crash, meanwhile, stopped at the recovery menu and kept everything.
  The default is now the recovery menu with the terminal alive, matching what
  the troubleshooting guide already documented. Closing is one more keypress
  and closing is not undoable, so the terminal is worth more than the
  keypress. `~/.sk_autoclose_on_clean_exit` restores the old behaviour for
  anyone who wants it, a non-zero exit stops at the menu whether or not that
  marker exists, and the previous `sk_keep_exit_menu` marker is gone
  repository-wide.

- A public install no longer registers hooks whose files it never received.
  The export manifest matched 187 of 220 tracked files, and the 33 it missed
  included every file under `extras/`: both intake hooks are registered by
  absolute path into the installed release, so a release built from that
  manifest installed cleanly, registered both, and then failed on every
  prompt. The manifest now ships `extras/**`, the supervisor-family tests, and
  the checker and end-to-end tests added this release — 224 exported files —
  and its header records the deliberate exclusions. The built tree was
  verified before shipping: the strict scan passes, 51 documentation links
  resolve, and 50 test modules collecting 1,115 tests load with no errors.

- The Linux watchdog no longer installs dead. `session-kit services enable`
  enabled `shpool.socket` and `shpool-reaper.timer` and never
  `session-kit-watchdog.service`, so every Linux install carried a watchdog
  unit that was present, disabled, and inert. `enable`, `disable`, and
  `status` now cover it.

- Reproducible release archives are reproducible across Python versions.
  `build-release-artifact` ordered archive entries by sorting `Path` objects,
  which defers to the interpreter's own path comparison; two runners on
  different Python versions could therefore produce byte-different archives
  that each verified as reproducible against themselves, and CI would bless
  either. Ordering is now pinned to `PurePosixPath` parts, and the digest is
  proven unchanged for the current tree. The builder also refuses a commit
  that is not reachable, and re-opens and verifies the archive it has just
  written rather than trusting the write.

- An unchanged login picker no longer flashes every refresh cycle. Fleet
  Supervisor is now normalized into its rendered pinned position before the
  visible-state fingerprint is calculated, so the pre-render and rendered row
  orders cannot manufacture a false change. Compact response ages also omit
  the repeated `replied` label while `waiting` and `opened` fallbacks remain
  explicit.

- A message that landed is no longer reported as a retryable failure. When the
  headless sender delivered an envelope to a target and that target's own
  transcript has not shown it inside the check window, the receipt now says
  `landed-unconfirmed` rather than `failed` — a newborn or busy Claude session
  takes a queued message at its next turn boundary, minutes after the receipt
  is written. Delivery still counts only from the target's own transcript.

- Repeating a send no longer repeats the message. `SESSION_KIT_MSG_KEY` (or
  `msg send --key` on the core) names a purpose: a repeat resumes that
  purpose's message id, skips every target the ledger already shows as landed,
  and re-reads a Claude target's transcript before dispatching to it again.

- `bin/supervisor ensure` no longer re-injects the standing brief while the
  new supervisor is reading it. It waited for the word `delivered` in a
  receipt whose own headline always contains `not delivered`, so the test
  could never pass: every start sent the same brief up to twelve times and
  then declared it undelivered. Success is now the supervisor's own proof —
  a turn finished after the brief was sent, or the brief in its transcript —
  every attempt carries one idempotency key, and only a brief that never
  landed is sent again. Once the brief has landed the remaining attempts are
  spent waiting rather than sending, so a newborn that needs two minutes to
  reach its first turn boundary is a normal start, not a warning. A brief
  that landed with no turn behind it after the whole budget exits 3 and says
  so instead of being delivered a second time.

- `supervisor ensure` no longer reports success for a supervisor that was
  never briefed. The identity marker is published before the brief exists —
  terminal numbering needs it — so a start that failed to prove the brief
  left a marker that made every later `ensure` exit 0 without sending or
  proving anything. Being briefed is now its own durable record, and an
  identity without it re-enters the injection and proof loop against the same
  session. A finished turn also no longer counts as proof on its own: turns
  are caused by anything, so one counts only behind a receipt showing the
  brief reached that session, and the cutoff for "after the brief" is when
  the brief was actually sent, even if that was a previous run.

- Live Codex renames now reach the newest App Server sockets. The sweep
  visited directories oldest-first under a cap of eight, so enough abandoned
  socket directories starved every live one; it now walks newest-first, skips
  dead sockets on a fast refused connect, and counts only accepted
  connections against the cap. The reaper also removes socket directories
  whose session is gone, whose socket refuses, and which nothing has touched
  for an hour.

- Process scans no longer read `/proc/<pid>/environ` or `/proc/<pid>/cwd` for
  processes another user owns. Both are readable only by the process owner, so
  those reads always failed and the caller fell back to the same empty values
  the scan now uses directly. The inventory asks only for processes it owns
  (root excepted) and the reaper reads `environ` only for the daemon's direct
  children, which are the only ones it ever consults. On a host with hundreds of
  processes this removes upwards of two thousand failed system calls per sweep;
  where the kernel is auditing failed opens, each of those was also a log record.

- Commands no longer retry a bytecode cache write into the release directory.
  A release is installed read-only, so `__pycache__` could never be created
  there and every import attempted it anyway. `PYTHONDONTWRITEBYTECODE` is now
  set where the entry points converge. Nothing that was cached stops being
  cached, because nothing ever was, and a `.pyc` precompiled by a future install
  step is still read.

### Security

- Source-authority verification refuses harness machine text and can finally
  verify Codex sessions. A prompt that begins as a harness envelope
  (`<task-notification`, `<cross-session-message`, `<system-reminder`, and ten
  more corpus-evidenced tags) is machine text that reached the same hook, and
  is now refused both at capture and again at verify time, so envelope events
  already recorded as capable can no longer certify — on one live install, 32
  of 82 "capable" events were such envelopes. Codex events, which had never
  once verified, now can: an empty `transcript_path` resolves to the session's
  unique rollout under `CODEX_HOME` (anchored filename match, symlinks refused,
  a second candidate refuses rather than chooses), and provider-owned
  transcripts are verified under a no-foreign-write mode rule (owner uid, no
  group/other write bit) instead of the kit-state owner-private rule Codex's
  0644 rollouts could never meet. Kit-owned state and Claude transcripts keep
  the strict rule unchanged.

- Every test-only environment hook is gated behind `SESSION_KIT_TESTING`. Five
  hook families reached fourteen production sites and eleven of them were
  honoured unconditionally, so a process able to set one environment variable
  could reach them. The largest was `SESSION_KIT_PROC_ROOT`, ungated in seven
  places: `/proc` is where every identity proof in the kit gets its answer —
  which processes exist, when each started, what each is running, who its
  parent is — so the variable let any caller hand-author that evidence,
  including the evidence the reaper uses to decide a session is dead. The
  lifecycle failpoint's fatal branch also fired ungated, which made an
  installer denial of service one variable wide, and two JSON-file hooks could
  substitute the whole session roster. All eleven are gated at the lowest
  shared point of each dependency island, and a gate that refuses a hook falls
  back to real evidence instead of dying. A new test proves every gate both
  closed and open, and a regression test walks the tracked production files
  and fails if a hook name is ever read again without a gate beside it.

- Personal identifiers no longer ship. The maintainer's name was present as
  prose and, less visibly, as public API surface: a supervisor ledger key, an
  MCP `send_message` schema property, receipt strings, and the `From:` line of
  the envelope injected into every messaged agent. Those are now
  `operator_confirmed` and role wording throughout. Test fixtures carried real
  email addresses and account aliases; addresses are now `@invalid.example`
  and the aliases are neutral. `tools/public-scan` digests the name and alias
  tokens and also hashes the parts of underscore-separated identifiers, so an
  identifier compound can no longer hide a private word inside it — seeded
  files that passed the old scanner are refused by this one. `CONTRIBUTING.md`
  states the rule the export gate enforces: roles rather than people, neutral
  fixture aliases, and `@invalid.example` addresses.

### Added

- `tools/publish-release`, which makes a release one verified transaction from
  source commit to artifact. The 0.2.0 tag and the 0.2.0 artifact were built
  from different commits and nothing compared them; the artifact's recorded
  source commit is now an unreferenced object that has been collected, so that
  release cannot be reproduced or audited at all. The failure was not inside
  any one tool but in the manual sequence between them, which is why the fix
  is a single command rather than a longer checklist. One invocation exports
  one source commit, syncs that exact tree into the public repository as one
  commit, annotates a tag on it, builds the artifact from the same source
  commit, and then proves rather than assumes that the tag and the artifact
  are the same bytes: the blob set of the tagged commit must equal the export,
  the files inside the archive must equal the export, and the `SOURCE.json`
  recorded inside both must name the release commit. Ten named gates each fail
  closed, and a printed `session-kit-<version>.chain.json` binds version,
  source commit, public commit, previous public head, tree digest, and archive
  digest. `--dry-run` rehearses the whole sequence in throwaway clones and
  only reads the real public repository. `--push` requires an interactive
  terminal and a typed confirmation, and refuses outright if any override was
  used, so a published release can never carry one.

- `tools/check-embedded-scripts`, which checks the code that lives inside
  other code. It extracts all 116 heredoc bodies from the 34 shell files, runs
  each through the right compiler for its language, and maps any error back to
  its real line number in the containing file. CI runs it on Linux and macOS.

- Health checks for the conditions under which the previous doctor reported
  healthy while sessions could not survive a logout. Doctor now verifies that
  the watchdog unit is enabled and active rather than merely installed, that
  logind lingering is on, that the installed `shpool` meets the pinned 0.11.0,
  the enabled and active state of each socket and timer unit, and that every
  registered hook file exists and is executable at the exact path the
  registration runs. Every failure row carries a one-line fix command.

- The third-party notices the repository owed. Vendored Maniple files named
  their upstream source and commit but shipped no copyright notice and no MIT
  permission text, which is the part MIT actually requires to be included.
  Upstream publishes no `LICENSE` file at the pinned commit, so the notice
  reproduces the standard MIT text with the holder taken from upstream's own
  declared authors and says so explicitly, and it records the upstream URL,
  the pinned commit, the file-by-file mapping, and the substantive
  modifications. Every `shpool-patch` file — not only `0001` — now opens with
  the project, the Apache-2.0 copyright, the base revision `fe2d115`
  (shpool 0.11.0), and what it changes; all four were re-proven to apply
  cleanly to a fresh upstream clone with byte-identical statistics.

- Local Claude Code and Codex account profiles. Each profile isolates native
  provider state through its own `CLAUDE_CONFIG_DIR` or `CODEX_HOME`; Session
  Kit records account descriptions but never copies or logs provider tokens.
  `sp account list`, `enroll`, and `verify` manage profiles, while `sp new
  ... --account <alias>` selects one explicitly. Guided New may preselect a
  current provider-qualified Matrix recommendation, but currently leaves Codex
  unselected because that advice is not yet provider-qualified. Existing
  sessions can change account only after confirmation and fresh exact-identity,
  idle, and no-child-work checks. The exact conversation, history, title,
  project, and terminal number are retained; a session created before this
  feature needs one explicit recreation of its managed shell. Failed changes
  attempt a checkpointed return to the original profile. The owner-only
  `account-switching-off` state sentinel can disable the feature without
  stopping providers, and no live-thread account rotation is automatic.

- Compact picker rows now use a fixed three-letter provider column (`CLD`,
  `CDX`, `SHL`, or `UNK`), with yellow Claude and cyan Codex labels. Provider,
  state, and time columns share fixed starts so mixed `idle`, `working`, and
  `needs your reply` rows remain vertically aligned.

- The login picker now shows when each AI session last replied in both
  `Ready to open` and `Open elsewhere`. A session waiting on the operator shows
  how long it has waited instead; shells and sessions without response history
  show their opened age with that fallback named explicitly. Relative times use
  readable minutes, hours, and days before switching to a date, narrow terminals
  retain the title and primary state first, and `sp detail <number>` carries the
  exact local response and opened timestamps.

- `sp msg`: the message centre. One screen writes to every live session,
  every idle one, or a single one, shows deterministic per-target delivery
  receipts, streams replies in as they arrive, and answers any target in
  place. The picker opens it with `s` and shows `✉ N new replies` while any
  reply waits. Direct sends (`sp msg all "text"`) exist for scripted or
  one-line use. Docs: `docs/messaging.md`.
- A project reaches the fleet supervisor without anybody messaging it. When a
  root session takes its first substantive prompt, the provider hook records
  that prompt as an intake and starts `supervisor ensure` detached: no agent
  cooperates, no instruction is added to any agent, and the human's prompt
  waits for none of it — the entry is durable before the supervisor is asked
  for, and a supervisor that cannot start costs nothing. One root thread
  yields exactly one OPEN intake however often the hook fires (the claim is the
  thread itself); subagents and sidechains yield none; slash commands,
  greetings, agreement, resume boilerplate, and the kit's own operator
  envelopes are not projects. Simultaneous root starts share one `ensure`
  through a non-blocking lock and a one-minute stamp. Both providers are picked
  up by the same `UserPromptSubmit` hook in front of the first turn —
  `extras/hooks/sk_session_events.py` for Claude,
  `extras/hooks/sk_codex_intake.py` for Codex, the latter as a user-level
  `~/.codex/hooks.json` command hook, which loads even in an untrusted project
  and is trusted by the exact hash of the file. Both adapters' logic is tested
  against fixture payloads; no live Codex process has been observed discovering
  and trusting the hook, and that proof is deferred to the end-to-end drill.

- A project that grows reaches the supervisor as it grows. The first
  substantive prompt in a root thread opens the intake; every later one, while
  that intake is open, is appended to it as a durable amendment — keyed by turn
  and prompt digest, so a hook firing twice records one — and delivered to the
  supervisor exactly once through the messaging core's idempotency key, in a
  detached run that never delays the prompt that caused it. An amendment
  extends the open project: it never opens a second intake, and never asks for
  a second delegation. A prompt arriving after the project was reported opens a
  fresh intake linked to the one it follows.

- `sp msg intake`: the fleet supervisor's intake spool. A project handed to the
  supervisor as a message is written to
  `~/.local/state/session-kit/supervisor/intake/` before anything is delegated,
  so a resident that dies or is refreshed mid-project loses its conversation
  and nothing else. One entry per intake carries the lifecycle (received →
  acknowledged → delegated → reported), the worker branches, and every note
  relayed to the source thread. A second arrival of the same intake — the same
  message id, or the same intake key under a new one — joins that entry instead
  of starting a second project, and `msg intake open` gives a replacement
  resident the unfinished ones on its first read. Progress and completion go
  out through the existing `sp msg` delivery path under the note's own
  idempotency key; a note that never landed stays owed rather than reported.
  Docs: `docs/supervisor.md`.

- `codex_app_server_all` in `~/.config/session-kit/coordination.json` arms the
  Codex App Server socket — the thing that makes a Codex session messageable —
  for kit launches in every project, not only the coordination repository. The
  repository-scoped broker behavior is unchanged, and the private state-root
  check now says on stderr when it disables the App Server instead of falling
  back silently.

### Changed

- Static checks now cover the code they previously skipped. `ruff` and
  `py_compile` reach the 2,186-line release engine that no check had ever
  read, along with the new checker and the dashboard renderer, and the `mypy`
  target grows from 24 files to 58 by adding the supervisor, messages, and
  events packages and the provider hooks.

- `docs/maintainers/release-process.md` describes the tool rather than the
  manual steps. The six-step publish sequence it used to document is the exact
  procedure that let a tag and an artifact diverge; it now documents the single
  `publish-release` invocation, what each gate refuses, and how to undo a
  prepared release that has not been pushed.

- The supervisor ledger and the MCP `send_message` schema name the operator
  confirmation with a role rather than a person. This is a wire and storage
  rename, not only wording: a ledger row written before this release records
  the old key, so it now reads as unconfirmed and counts toward the autonomous
  turn budget. That fails in the restrictive direction rather than the
  permissive one, the ledger is append-only, and a one-shot local migration
  follows the release install for anyone who wants the earlier rows to keep
  counting as confirmed.

- The CI quality job is green at the release commit: shellcheck, `ruff
  check`, `ruff format`, and mypy (58 files, no per-module overrides) all
  pass. The sweep proved one flagged import was load-bearing for
  `sp msg queue --mark-seen` and kept it as an explicit re-export, bound the
  supervisor's confirmed-act scope check so the narrowing survives, and added
  a regression test that fails if that validation is ever removed. A new
  manifest-coverage test fails the build when a tracked file is neither
  exported nor on the documented private list, and the delegation end-to-end
  test now terminates the sandboxed shpool daemon it spawns instead of
  leaking it into the host process table.

## [0.2.0] - 2026-08-06

### Changed

- Split `lib/session_inventory.py` into `lib/sessionkit_inventory/`. The file
  held configuration, process inspection, provider discovery, inventory
  assembly, state, naming, recovery, rendering, and CLI parsing in one place;
  it now holds CLI parsing, `main`, and compatibility wrappers, and is 2,905
  lines rather than 7,008 across twenty-one focused modules.

  No behaviour changes. The facade stays executable and importable by path with
  identical symbols, signatures, exit codes, JSON fields, and rendered output;
  no package module imports it; the dependency graph is acyclic; and imports
  perform no scans, locks, configuration reads, or state writes.

  The defect this work had to defend against is silent rather than loud. When a
  package module resolves a sibling symbol directly, patching that name on the
  facade reaches nothing: the patch applies against nothing and the test stays
  green against the real implementation. Constants are the worst case, because
  a differential run with every constant at its real value is clean by
  construction. Three constants reached zero reachability and were caught only
  by patching each one and observing whether behaviour moved.
  `docs/maintainers/modularization-roadmap.md` records the compatibility
  contract and a hand-triaged ledger of the names deliberately resolved inside
  the package, with the module to patch for each.


### Changed

- Split the session palette in two, one per provider, and stopped a same-colour
  repeat from surviving inside either. Two live rows could show one colour for
  two separate reasons, and fixing one left the other. Claude and Codex drew
  from a single eight-name palette, so they collided across providers; and the
  colour comes from an identity hash, which collides on its own well before the
  names run out — measured on live state, eight Claude sessions landed on seven
  colours, two on pink with blue unused.

  Claude Code's `/color` accepts exactly `red`, `blue`, `green`, `yellow`,
  `purple`, `orange`, `pink`, and `cyan`. Twenty-two names were probed against
  Claude Code 2.1.223 with known-good and known-bad controls; every other name
  is rejected, and `gray`/`grey` resolve to `default`, which is no colour. That
  palette is therefore fixed from outside. Codex resolves its colour from an
  `sk-*.tmTheme` file this kit ships and applies no allow-list, so it now has
  six names of its own — `lime`, `magenta`, `silver`, `sand`, `sky`, `sea` —
  that Claude Code cannot use. A Claude window and a Codex window can no longer
  land on the same colour at all.

  Within a palette, a session keeps its identity-hash colour unless a live
  session of the same provider already holds it, in which case it takes the
  next free name in palette order. When every name is taken the hash colour
  applies again and the repeat is allowed, because there is no free colour to
  give; the result stays a function of identity rather than of arrival order,
  so a session that has to share shares with the same partner every time.

  Existing Codex sessions carrying a stored override that names a colour no
  longer in the Codex palette need no migration pass. The override stops
  matching the in-force palette, so it is ignored and the session re-hashes
  into the palette that now applies.

- Release payload schema 5 requires `lib/sessionkit_inventory/colors.py`, which
  is where both palettes are now declared. Schemas 1 to 4 are unchanged, so a
  rollback still validates the payload a pinned older release shipped.

### Added

- `sp color reconcile`, which settles every live same-provider colour collision
  in one pass instead of waiting for each session to relaunch into the new
  palette. It writes an override only where palette order moved a session off
  its own identity hash, pushes each moved colour so an open Claude window
  picks it up at its next start or resume, and clears stored colours that name
  a colour outside the palette now in force. Repeating it changes nothing: rows
  are settled in a fixed identity order and each prefers the colour it already
  shows, so the second pass finds every preference free and writes no file.

- Six Codex theme files, `sk-lime`, `sk-magenta`, `sk-silver`, `sk-sand`,
  `sk-sky`, and `sk-sea`. The eight Claude-named themes stay shipped so a
  rollback to 0.1.6 still finds every theme it installs.

- A `shpool-binary` check in `session-kit doctor`. The watchdog already
  compared the running daemon against a recorded fingerprint, but nothing
  reported whether that comparison could still tell you anything. Both ways
  it goes inert are silent: with no fingerprint recorded the check is
  skipped, and with a stale one it reports a changed binary on every pass
  for a rebuild you made on purpose, until the report stops being read.
  Doctor now distinguishes absent, malformed, and no-longer-matching,
  because the remedy differs for each. The shpool patch guide gives the
  exact command to record it, which it previously left to the reader.

- Documentation rewritten across every page. Corrections rather than polish:
  the release badge linked to `releases/latest`, which returns 404 for every
  release this project has cut because beta releases are prereleases; the
  install command was pinned to a superseded tag; the architecture page still
  described the module split as in progress; the shpool patch guide asked the
  reader to record a fingerprint without naming a path or command, which is why
  one was left stale on a live installation; and `configuration.md` documented
  three watchdog variables in prose while its supported-overrides list implied
  they were unsupported. The troubleshooting guide gained the failure where
  every session becomes unreachable at once, which had no entry despite being
  the one condition no Session Kit command can diagnose, since every command
  blocks for the same reason the sessions do.


## [0.1.6] - 2026-08-06

### Fixed

- Added optional shpool patch `0004`, which fixes a detach deadlock in shpool
  0.11.0 that can make every managed session unreachable at once. Upstream
  `handle_detach` holds the global session-table lock across an unbounded send
  and receive on two rendezvous channels, so one client whose socket has stopped
  draining parks the lock for every other session. The patch resolves under the
  lock, drops it, performs a bounded handshake, then re-locks briefly for
  bookkeeping, matching the pattern upstream already uses elsewhere in the same
  file. It applies to pristine `v0.11.0` independently of patches `0001`-`0003`.

### Changed

- Corrected the write-up for patch `0001`. It addresses heartbeat acknowledgement
  timeouts and would not have prevented the detach deadlock; the notes now say so
  and point readers at `0004` first.
- The watchdog now distinguishes an unset notifier from a broken one. With
  `SESSION_KIT_WATCHDOG_NOTIFY` unset it logged that the empty string was not
  executable, which reads like a misconfigured path rather than an absent
  configuration. Documented the variable, including that leaving it unset means
  no alert reaches anyone.

## [0.1.5] - 2026-08-05

### Changed

- Ran the full picker and provider-exit test suites on macOS CI through the
  native Darwin process adapter, replacing a Linux-only harness that read
  process generations straight from `/proc`; the one test that genuinely
  needs `/proc` now skips on Darwin instead of failing.

### Fixed

- Removed a picker repaint guard that conditioned redraws on a terminal input
  probe which always reports an empty queue in canonical mode; repaints never
  consume queued characters, so a half-typed search now survives a live menu
  repaint on Linux and macOS alike.
- Allowed read-only install and doctor probes to reach the current user's
  systemd manager through its documented local-machine transport when the
  direct private socket is unavailable, while reporting the degraded socket as
  a warning and leaving service-control commands fail-closed.
- Made Claude's persisted `agent-name` record outrank its later generated
  window label, so an exact `sp self-name` converges to ready instead of
  remaining pending after a successful native write.
- Reserved and persisted an exact live-palette color after a failed Claude
  pre-bake, before the detached session can be attached, instead of falling
  back to a collision-prone identity hash.
- Added warning-only migration audits for bounded provider versions, private
  Codex themes, naming instructions and hooks, active kill switches, and the
  private release acceptance record.
- Excluded inaccessible provider project paths while retaining readable shared
  repositories, and honored `CODEX_HOME` consistently during discovery and
  theme installation.

## [0.1.4] - 2026-08-03

### Added

- Added bounded first-install discovery and import of existing Claude Code and
  Codex project folders, plus rerunnable `session-kit projects` commands.
- Added collision-safe provider-specific aliases, owner-only backups, and
  explicit noninteractive import control.

### Fixed

- Associated a managed Codex App Server's single open remote-TUI rollout with
  its Session Kit terminal, restoring the exact thread title and launch color.
- Kept editor rollouts ineligible for ordinary Codex processes and refused
  ambiguous app servers with multiple open threads.
- Deferred Codex in-window title refreshes while the exact provider is attached
  or working instead of labeling an unsafe restart as immediately pending.
- Derived Codex child-thread activity from each exact rollout so completed
  subagents no longer inflate the active count.
- Kept the internal Codex bar-refresh marker out of normal dashboard rows when
  the saved session title is already correct.
- Targeted an App Server title refresh at its exact remote TUI instead of the
  server process, preserving the resume socket and managed thread.

## [0.1.3] - 2026-08-03

### Fixed

- Ignored stale Claude agent records whose process no longer has a live Darwin
  generation, so an exiting provider cannot block later guarded session
  actions on macOS.

## [0.1.2] - 2026-08-03

### Fixed

- Moved every runtime `mktemp` suffix before the replacement characters so
  snapshots and proof files are unique on BSD `mktemp` as well as GNU
  `mktemp`.
- Restored macOS open, takeover, close, prune, reaper, watchdog, login, and
  provider-proof paths that could otherwise reuse a literal `XXXXXX` filename
  or fail after the first call.
- Added a repository-wide regression that rejects any runtime `mktemp`
  template with characters after its final `XXXXXX`.

## [0.1.1] - 2026-08-03

### Fixed

- Preserved pipeline input through the cross-platform timeout wrapper so the
  hidden Claude bootstrap receives its native `/color` command on Linux and
  macOS.
- Reconciled Claude transcript auto-titles with the provider's `agent-name`
  record without replacing an explicit `/rename`.
- Passed the hydrated Claude name through the native `--name` option when an
  exited provider is reopened.
- Kept the dashboard title state pending until the live Claude process reports
  the same visible name; an already-running Claude TUI is never reported as
  repainted by an external storage write.

## [0.1.0] - 2026-08-03

### Added

- Unified local inventory for shpool, Claude Code, Codex, and shell sessions.
- SSH picker with boot-scoped terminal numbers and task-focused names.
- Structured `needs your reply` and optional-reply states.
- Exact provider resume, recovery, and fork operations.
- Optional local terminal journals, off by default.
- Guarded open, move, close, repair, and cleanup actions.
- Immutable installed releases with update and rollback.
- Read-only install preflight, doctor, login enable and disable, and uninstall.
- Report-only local health checks.
- Native Linux `/proc` and Darwin process-identity adapters.
- Transactional install, update, rollback, and uninstall on Linux and macOS.
- Per-user launchd definitions and explicit service lifecycle commands on
  macOS.
- Reachable-history privacy scanning, public-export completeness checks, and
  reproducible release archives with checksum and provenance files.

### Changed

- Made terminal journals opt-in and off by default.
- Kept the managed shpool terminal alive after Claude Code or Codex exits.
- Added a clear provider-exited state with exact reopen, keep, shell, and close
  choices.
- Limited automatic close to exact provider-exited terminals that satisfy every
  safety predicate continuously for 72 hours after timer enablement.
- Simplified dashboard wording and applied consistent semantic color categories.
- Removed internal IDs from normal rows while keeping them in detail, JSON, and
  explicit search views.
- Made `k <number>` the documented close shortcut with an exact-target safety
  display and no extra prompt.
- Reduced normal log data and documented private reporting requirements.
- Required macOS 14 or newer, Python 3.11 or newer, Homebrew Bash 4 or newer,
  and shpool 0.11.0 for the macOS beta.

### Known limitations

- macOS services require the same user to be logged into the Mac desktop; the
  beta does not install a privileged or headless system daemon.
- Watchdog repair requires Linux daemon-thread evidence. The macOS watchdog is
  report-only.
- Provider storage formats and command interfaces remain version-sensitive;
  versions outside the release acceptance evidence are best-effort.
