# Update and roll back

Session Kit installs from reviewed local source and never fetches it. Release
selection does not disturb shpool or its live sessions. As part of install,
update, and rollback it does refresh the service manager safely: on Linux it
reloads unit definitions, may enable a newly introduced timer, and restarts an
already running watchdog; on macOS it kickstarts an already loaded watchdog.
It never starts, stops, or restarts the shpool daemon in that transaction.

Review and test an updated local Git checkout first. From that checkout, run
`session-kit update --source "$PWD"`, then run `session-kit doctor`.

`session-kit update` can omit `--source` when the source recorded by the current
install receipt is still available. If that path no longer exists, give it a
reviewed local Git checkout or a supported extracted release source explicitly.

## Updating does not disturb running sessions

This is the property the whole layout exists to provide, so it is worth being
precise about why.

```text
$HOME/.local/lib/session-kit/
  current -> releases/<full-commit-id>
  releases/<full-commit-id>/
$HOME/.local/bin/
  session-kit
  kit
  sp
  shpool_login
  shpool_login_tui
  shpool_login_launcher
  shpool_status
  shpool_reaper
  codex_resume_here
```

Each release directory is immutable and named for its exact source commit. The
files in `$HOME/.local/bin` are stable executable launcher copies, not symlinks
into a release. A launcher resolves `current` once and pins that physical
release for the lifetime of the command. A command already running therefore
finishes on the release it started with, and only new commands use the newly
selected one.

Lifecycle commands use the separately pinned `manager` release. A rollback can
therefore select an older runtime without reviving that runtime's obsolete
updater. On update, the current manager validates the reviewed source and hands
the transaction to that source's manager, so new transaction rules apply on
the first update rather than one update later.

`session-kit update` checks compatibility, installs the exact source commit,
keeps the prior release, refreshes the stable launchers, and switches the
pointer atomically. Release selection does not restart, stop, signal, attach
to, or detach from shpool. Your open terminals keep running.

Updates preserve the current journal choice. Supply `--journal on` or
`--journal off` only when you mean to change it. The existing login-integration
choice is also preserved unless you supply an explicit login flag.

Claude profiles are handled independently. If a profile's `settings.json`
cannot be safely parsed or written, the command names that file once, skips its
registrations, and continues the release change; rollback is never held up by
an unreadable or invalid provider settings file. The one exception is a
symlinked `settings.json`: the kit never edits through a symlink, so it names
the link and stops; replace the link with a regular copy and rerun. Install and update refuse to replace an
unrelated `statusLine` by default. After reviewing the named profile, rerun with
`--force` to preserve its current value and install the kit status line.

The owner-private state file `claude-statusline-backups.json` records the
operator value that uninstall restores. `claude-integration.json` records the
installed Claude hook and status-line files by digest, so uninstall removes
only unchanged kit copies and retains the record for an edited copy it leaves
in place.

## Service activation during update

On Linux, update runs `systemctl --user daemon-reload`. It enables a timer only
when that timer is newly introduced and `is-enabled` reports no prior policy;
an explicitly disabled timer stays disabled. It then uses `try-restart` for an
already running watchdog, so the watchdog begins using the selected release
without starting a service that was off. Read-only source and doctor probes can
fall back to systemd's local-machine transport when the direct user socket is
unavailable; service control still requires the normal direct user socket.

On macOS, update regenerates the owner-private LaunchAgent templates and
kickstarts the watchdog only when that label is already loaded. It does not
load a fresh install automatically.

Neither platform restarts shpool or the reaper. If one of those definitions
changed, reach a safe point with no managed sessions and apply it deliberately:

```bash
session-kit services disable
session-kit services enable
session-kit services status
```

`services disable` refuses to unload shpool while live sessions exist, or when
their absence cannot be proved. `services enable` refuses to overwrite a loaded
job whose active definition differs from the generated template.

## Roll back

`session-kit rollback` selects the prior installed release recorded by the last
install or update. For any other retained release:

```text
session-kit rollback --to <full-commit>
```

Rollback validates the retained release, updates the pointer, regenerates
platform service templates, updates the private integration marker and
receipt, and performs the same safe service refresh as update. It never
restarts shpool. A loaded shpool or reaper definition still needs the same safe
disable and enable cycle above when its definition changed.

The unit list belongs to the release that is running, so a rollback away from a
release that added a systemd unit reads a name the target release never
carried. On Linux that unit is not installed -- it does not exist to install --
and its file is removed from `~/.config/systemd/user/`, with the `systemctl
--user disable` line to run printed for it. Leaving the file would leave systemd
starting the new release's program out of the old release's tree. A unit path
that is a symlink is left alone rather than followed. Rollback is never refused
over a unit file: a release that adds a unit must not remove the way back past
itself.

The recovery-capable management launcher is stable across rollback. If power
is lost after the pointer changes, it reads the owner-private pending journal
and runs the exact pre-flip manager that created it. The launcher itself is not
rewritten after journal removal, eliminating the former kill window where an
older manager could become active without durable recovery instructions. Both
journal publication and removal are followed by a directory sync so the
decision survives a machine crash.

### When an old validator refuses a modern launch record

A retained old release may predate the remedy text and target-aware validation
used by the current manager. After rolling back, its validator can refuse a
newer launch record without explaining how to proceed. Do not delete or edit
the record to get past that refusal.

Use the **target release's own** retained
`deploy/session-kit-release` rollback program to complete the rollback under
the rules that release understands. The other safe choices are to wait until
the launch record ages out, or quarantine that exact record for diagnosis and
retry. Which option is safe depends on whether its provider process can still
be live; preserve the record until that has been established.

### Inventory publishing fence

Releases with the generation-2 inventory publisher replace
`inventory.lock` with an owner-only refusal sentinel and publish through
`inventory-v2.lock`. The sentinel prevents an already-loaded older collector
from replacing a newer whole-session list. A release that predates that code
cannot open the sentinel, so a fresh login picker under such a release receives
no list even though `inventory.json` is still present.

`session-kit rollback` inspects the retained target's `StateLock` code before
changing `current`. When that target has no generation-2 path, the currently
installed manager serializes with `inventory-v2.lock`, accepts only the exact
owner-owned mode-0400 sentinel, and writes an owner-only durable rollback hold.
While retaining `inventory-v2.lock`, it replaces the sentinel with a plain
mode-0600 `inventory.lock` and changes `current`. A generation-2 publisher that
was already waiting wakes after the flip, sees the hold, and refuses read-only
instead of recreating the sentinel behind legacy code. The hold remains while
the legacy release is selected. After a forward update, a newly launched and
currently selected generation-2 publisher retires it under the versioned lock
before recreating the refusal fence.

The crash boundaries deliberately leave only three pairs: generation-2 code
with the sentinel, generation-2 code with the plain lock, or legacy code with
the plain lock. The durable hold makes each pair retryable and prevents the
last pair from being re-fenced by an older in-flight publisher.

The automatic step is the supported path. Use the following manual un-fence
only after some older rollback mechanism has already selected a pre-generation-2
release and a newly opened picker cannot list sessions. Do not run a status
refresh concurrently. The script refuses a symlink, wrong owner or mode,
linked file, changed pathname, or lookalike content; it changes only the exact
sentinel:

```bash
state_dir=${SESSION_KIT_STATE_DIR:-${XDG_STATE_HOME:-"$HOME/.local/state"}/session-kit}
python3 - "$state_dir" <<'PY'
import fcntl, os, pathlib, stat, sys, tempfile

root = pathlib.Path(sys.argv[1])
legacy = root / "inventory.lock"
versioned = root / "inventory-v2.lock"
expected = b"session-kit publishing lock generation 2\n"
flags = os.O_RDWR | os.O_CREAT
read_flags = os.O_RDONLY
for name in ("O_CLOEXEC", "O_NOFOLLOW"):
    flags |= getattr(os, name, 0)
    read_flags |= getattr(os, name, 0)

def require(condition, message):
    if not condition:
        raise SystemExit(f"session-kit: refusing manual un-fence: {message}")

root_info = root.lstat()
require(
    stat.S_ISDIR(root_info.st_mode)
    and not stat.S_ISLNK(root_info.st_mode)
    and stat.S_IMODE(root_info.st_mode) == 0o700
    and root_info.st_uid == os.geteuid(),
    f"unsafe state directory: {root}",
)

lock_fd = os.open(versioned, flags, 0o600)
try:
    lock_info = os.fstat(lock_fd)
    lock_path_info = versioned.lstat()
    require(
        stat.S_ISREG(lock_info.st_mode)
        and stat.S_IMODE(lock_info.st_mode) == 0o600
        and lock_info.st_uid == os.geteuid()
        and lock_info.st_nlink == 1
        and (lock_info.st_dev, lock_info.st_ino)
        == (lock_path_info.st_dev, lock_path_info.st_ino),
        f"unsafe generation-2 lock: {versioned}",
    )
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    locked_path_info = versioned.lstat()
    require(
        (lock_info.st_dev, lock_info.st_ino)
        == (locked_path_info.st_dev, locked_path_info.st_ino),
        f"generation-2 lock changed: {versioned}",
    )
    sentinel_fd = os.open(legacy, read_flags)
    try:
        info = os.fstat(sentinel_fd)
        path_info = legacy.lstat()
        body = os.read(sentinel_fd, len(expected) + 1)
    finally:
        os.close(sentinel_fd)
    require(
        stat.S_ISREG(info.st_mode)
        and stat.S_IMODE(info.st_mode) == 0o400
        and info.st_uid == os.geteuid()
        and info.st_nlink == 1
        and (info.st_dev, info.st_ino) == (path_info.st_dev, path_info.st_ino)
        and body == expected,
        f"not the exact generation-2 sentinel: {legacy}",
    )
    replacement_fd, replacement = tempfile.mkstemp(
        prefix=".inventory.lock.", dir=root
    )
    try:
        os.fchmod(replacement_fd, 0o600)
        os.fsync(replacement_fd)
        os.close(replacement_fd)
        replacement_fd = -1
        os.replace(replacement, legacy)
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if replacement_fd >= 0:
            os.close(replacement_fd)
        try:
            os.unlink(replacement)
        except FileNotFoundError:
            pass
finally:
    os.close(lock_fd)
PY
```

After it succeeds, open a new picker. If it refuses, preserve both lock files
and the rollback receipt for diagnosis instead of deleting more state.

### Lost collection ordering state

The inventory publisher keeps an ordinary sequence counter and a separate
durable allocation floor. If one remains readable, the next refresh safely
rebuilds the pair above that record and above every readable collection marker.
If both are missing or unreadable while any published collection document or
marker remains, refresh stays read-only. Do not invent a sequence from the
documents: a collector that has not published yet may already hold a larger
one.

Install and upgrade prevent the common missing-floor case before selecting the
new release. When the floor does not exist, they copy the existing sequence
counter exactly. If the counter is empty, they seed the floor with `1`. An
existing floor is preserved byte for byte. If the missing floor cannot be
created safely, release selection stops and prints the exact reset command
below instead of leaving the new collector unable to publish.

If both records are permanently lost, run the one-command reset below from an
ordinary local shell immediately after a machine restart and before starting a
picker, `sp`, `shpool_status`, or any Session Kit user service. The restart is
what proves that no old collector still holds an unpublished allocation. The
command archives the six collection-derived documents, their markers, and both
sequence records; it does not delete them or touch session recordings,
transcripts, closed-session history, names, origins, account records, or project
settings. The next refresh is then a genuine first publication and starts at
sequence 1.

```bash
python3 ~/.local/lib/session-kit/current/bin/reset-collection-order.py
```

The command refuses unless the state directory and every entry it moves are
owner-only real files/directories, the allocation records are both unreadable,
and at least one lost collection-order entry exists.
It prints the owner-only archive directory. Keep that archive until the rebuilt
picker has been checked.

## State written by a newer release

A release that changes the format of private state must say whether an older
release can still read it, because rollback does not rewrite state.

**Session colors.** Current releases split session colors into a Claude palette
and a Codex palette. A retained older release ignores a stored color override
it does not recognize and falls back to its own palette; its validator drops
the unknown name rather than failing. A session that showed a newer color
simply returns to an older color. No state needs to be removed before rolling
back.

## shpool binary updates

Session Kit release selection and shpool binary replacement are separate
operations. Replacing shpool may require a daemon restart, which can end every
session process. Never combine that restart with a routine Session Kit
update.

Rebuilding or reinstalling shpool also discards any patch previously applied to
it, and that loss is silent. The optional patch has its own build and rollback
instructions in [the shpool patch guide](../shpool-patch/README.md).

## An interrupted update

Lifecycle commands record the exact pre-change file state before touching
exposed paths. A later Session Kit lifecycle invocation either completes a
committed transaction or restores an incomplete one.

Do not delete a transaction receipt to get past a refusal. Preserve it and
report the sanitized error: the receipt is what makes the recovery exact.
