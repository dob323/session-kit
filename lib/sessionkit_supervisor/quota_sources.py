"""Pluggable per-account quota readings for duty selection.

Choosing which account should run a duty needs a number, and the number has to
come from somewhere the kit can actually see. Three kinds of somewhere exist,
and they are not equally trustworthy:

* the provider published it — Codex writes a ``rate_limits`` block into every
  rollout it records, with a used percentage, the window length, and the reset
  time. That is the provider's own accounting, read straight off local disk.
* an operator-configured feed asserts it — the account roster the kit already
  reads for health and serving state carries ``u5h``/``u7d`` fractions. The kit
  cannot verify what produced them; it can only check that they are fresh.
* the kit inferred it — Claude Code publishes no quota figure locally, so the
  only local evidence is the transcripts themselves: tokens spent inside the
  window, and any 429 the provider returned.

A reading records which of those it is, so a caller can never mistake an
inference for a measurement. Nothing here converts token counts into a
percentage of an unknown allowance: an unknown fraction stays ``None`` and the
scorer says so out loud rather than inventing precision.

Sources are pluggable. ``register_source`` lets a deployment add a reader the
public kit does not ship — an authoritative allowances feed, a provider API
poller — without editing this module. Only local readers ship here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import stat
import time
from typing import Any, Callable, Iterable, Mapping, Sequence


FIVE_HOUR = "five_hour"
WEEKLY = "weekly"
WINDOWS = (FIVE_HOUR, WEEKLY)

# How a reading was obtained, best evidence first. The scorer prefers the
# strongest available reading for a window and records which one it used.
MEASURED = "measured"  # the provider itself published the number
FEED = "feed"  # an operator-configured roster asserted it
OBSERVED = "observed"  # the kit inferred it from local activity
DECLARED = "declared"  # a static plan or tier fact
CONFIDENCE_ORDER = (MEASURED, FEED, OBSERVED, DECLARED)

# A window whose length is at or below this is the short rolling window; longer
# is the weekly one. Codex reports 10080 minutes for its weekly limit and a
# shorter secondary window when its plan has one.
SHORT_WINDOW_MAX_MINUTES = 360

DEFAULT_MAX_AGE_SECONDS = 6 * 3600
DEFAULT_ROLLOUT_FILES = 12
DEFAULT_TRANSCRIPT_FILES = 40
DEFAULT_TAIL_BYTES = 512 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
WEEK_SECONDS = 7 * 24 * 3600
FIVE_HOUR_SECONDS = 5 * 3600
# Shortest span a load rate is computed over. A scan that reached back only a
# few seconds would otherwise divide a real total by almost nothing and report
# an account as impossibly busy.
MIN_COVERAGE_SECONDS = 60


class QuotaError(ValueError):
    pass


def _now(now: int | None) -> int:
    return int(time.time()) if now is None else int(now)


def _positive_int(name: str, default: int, environ: Mapping[str, str]) -> int:
    raw = environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _fraction(value: Any) -> float | None:
    """Return a 0..1 fraction, or None for anything that is not one.

    Feeds have shipped both spellings of "how much is used": 0.27 and 27. A
    used share above 1 cannot mean anything else, so it is read as a
    percentage. Clamping it to 1.0 instead would report a barely-touched
    account as fully spent and quietly take it out of the running.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number < 0.0:  # NaN or negative
        return None
    if number > 1.0:
        number = number / 100.0
    return min(number, 1.0)


def _unix(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    stamp = int(value)
    return stamp if stamp > 0 else None


def _iso_to_unix(value: Any) -> int | None:
    """Parse the RFC 3339 stamps both providers write into their records."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return int(datetime.fromisoformat(text).timestamp())
    except (ValueError, OverflowError):
        return None


@dataclass(frozen=True)
class QuotaReading:
    """One window's state for one account, with its provenance attached."""

    provider: str
    account: str
    window: str
    source: str
    confidence: str
    observed_at_unix: int
    used_fraction: float | None = None
    resets_at_unix: int | None = None
    load_units: float | None = None
    load_unit: str = ""
    exhausted: bool = False
    detail: str = ""
    model_hint: str = ""

    def __post_init__(self) -> None:
        if self.window not in WINDOWS:
            raise QuotaError(f"unknown quota window: {self.window}")
        if self.confidence not in CONFIDENCE_ORDER:
            raise QuotaError(f"unknown quota confidence: {self.confidence}")

    def stale(self, now: int, max_age: int) -> bool:
        return now - self.observed_at_unix > max_age

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "account": self.account,
            "window": self.window,
            "source": self.source,
            "confidence": self.confidence,
            "observed_at_unix": self.observed_at_unix,
            "used_fraction": self.used_fraction,
            "resets_at_unix": self.resets_at_unix,
            "load_units": self.load_units,
            "load_unit": self.load_unit,
            "exhausted": self.exhausted,
            "detail": self.detail,
            "model_hint": self.model_hint,
        }


@dataclass(frozen=True)
class AccountRef:
    """An account a duty could run on, and where its provider state lives."""

    provider: str
    alias: str
    email: str = ""
    home: Path | None = None
    enabled: bool = True
    registered: bool = True
    plan: str = ""

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.alias}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "alias": self.alias,
            "email": self.email,
            "home": str(self.home) if self.home else "",
            "enabled": self.enabled,
            "registered": self.registered,
            "plan": self.plan,
        }


def _accounts_module() -> Any:
    """Import the inventory's account registry from *this* package's root.

    The kit runs with ``lib`` on the path and the tests run with the repo root
    on it, so the same file answers to two import names. Trying a fixed order
    is not good enough: whichever name is tried first wins, so once anything in
    the process has imported the other spelling, Python holds *two* module
    objects for one file — two sets of module state, and a caller patching one
    of them has no effect on the other.

    Resolving the sibling relative to our own package removes the ambiguity.
    Imported as ``lib.sessionkit_supervisor``, we reach
    ``lib.sessionkit_inventory``; imported as ``sessionkit_supervisor``, we
    reach ``sessionkit_inventory``. Either way it is the copy our own caller is
    already using. The remaining names stay as a fallback for an unusual
    layout. A missing inventory is not an error: readers that do not need it
    still work.
    """
    from importlib import import_module

    package = __package__ or ""
    root = package.rsplit(".", 1)[0] if "." in package else ""
    names = [
        f"{root}.sessionkit_inventory.accounts"
        if root
        else "sessionkit_inventory.accounts"
    ]
    names += ["sessionkit_inventory.accounts", "lib.sessionkit_inventory.accounts"]
    for name in dict.fromkeys(names):
        try:
            return import_module(name)
        except ImportError:
            continue
    return None


def _provider_home(provider: str, environ: Mapping[str, str]) -> Path:
    home = Path(environ.get("HOME") or Path.home())
    return home / (".claude" if provider == "claude" else ".codex")


def account_refs(
    config: Mapping[str, Any],
    *,
    providers: Sequence[str] = ("claude", "codex"),
    environ: Mapping[str, str] | None = None,
) -> list[AccountRef]:
    """List every account a duty could be placed on, both providers alike.

    Registered isolated profiles come first. A provider with no registered
    profile still has the signed-in state in its home directory, and leaving it
    out would silently make that provider unschedulable — which is exactly the
    Claude-only bias this engine exists to remove.
    """
    values = environ if environ is not None else os.environ
    module = _accounts_module()
    refs: list[AccountRef] = []
    for provider in providers:
        found = False
        if module is not None:
            try:
                profiles = module.list_profiles(config, provider)
            except Exception:
                profiles = []
            for item in profiles:
                found = True
                home = item.get("profile_dir") or ""
                refs.append(
                    AccountRef(
                        provider=provider,
                        alias=str(item.get("alias") or ""),
                        email=str(item.get("email") or ""),
                        home=Path(home) if home else None,
                        enabled=bool(item.get("enabled")),
                        registered=True,
                        plan=str(item.get("plan") or ""),
                    )
                )
        if not found:
            home = _provider_home(provider, values)
            if home.is_dir():
                refs.append(
                    AccountRef(
                        provider=provider,
                        alias="default",
                        email="",
                        home=home,
                        enabled=True,
                        registered=False,
                    )
                )
    return refs


def _safe_regular_file(path: Path, max_bytes: int = MAX_FILE_BYTES) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return (
        not stat.S_ISLNK(info.st_mode)
        and stat.S_ISREG(info.st_mode)
        and info.st_size <= max_bytes
    )


def _tail_lines(path: Path, tail_bytes: int) -> list[str]:
    """Read at most ``tail_bytes`` from the end of a record file.

    Provider record files grow without bound; a selection decision must not
    cost a full read of every transcript on the box. The first line of a tail
    is usually a fragment, so it is dropped.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > tail_bytes:
                handle.seek(size - tail_bytes)
                partial = True
            else:
                partial = False
            payload = handle.read(tail_bytes + 1)
    except OSError:
        return []
    text = payload.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if partial and lines:
        lines = lines[1:]
    return lines


def _newest(paths: Iterable[Path], limit: int) -> list[Path]:
    scored: list[tuple[float, Path]] = []
    for path in paths:
        try:
            scored.append((path.stat().st_mtime, path))
        except OSError:
            continue
    scored.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in scored[:limit]]


class CodexRateLimitSource:
    """Read the rate-limit block Codex records in its own rollout files.

    Codex writes a ``token_count`` event after every turn carrying the limits
    the service reported: used percent, window length, reset time. It is the
    strongest quota evidence available locally for either provider, because the
    provider produced the number rather than the kit inferring it.
    """

    name = "codex-rate-limits"

    def __init__(self, *, files: int | None = None, tail_bytes: int | None = None):
        environ = os.environ
        self.files = files or _positive_int(
            "SESSION_KIT_QUOTA_ROLLOUT_FILES", DEFAULT_ROLLOUT_FILES, environ
        )
        self.tail_bytes = tail_bytes or _positive_int(
            "SESSION_KIT_QUOTA_TAIL_BYTES", DEFAULT_TAIL_BYTES, environ
        )

    def read(self, ref: AccountRef, *, now: int) -> list[QuotaReading]:
        if ref.provider != "codex" or ref.home is None:
            return []
        sessions = ref.home / "sessions"
        if not sessions.is_dir():
            return []
        candidates = [
            path
            for path in sessions.rglob("rollout-*.jsonl")
            if _safe_regular_file(path)
        ]
        for path in _newest(candidates, self.files):
            readings = self._from_file(ref, path, now=now)
            if readings:
                return readings
        return []

    def _from_file(
        self, ref: AccountRef, path: Path, *, now: int
    ) -> list[QuotaReading]:
        for line in reversed(_tail_lines(path, self.tail_bytes)):
            if "rate_limits" not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            payload = record.get("payload")
            if not isinstance(payload, Mapping):
                continue
            limits = payload.get("rate_limits")
            if not isinstance(limits, Mapping):
                continue
            observed = _iso_to_unix(record.get("timestamp")) or now
            readings = self._from_limits(ref, limits, observed=observed, path=path)
            if readings:
                return readings
        return []

    def _from_limits(
        self,
        ref: AccountRef,
        limits: Mapping[str, Any],
        *,
        observed: int,
        path: Path,
    ) -> list[QuotaReading]:
        readings: list[QuotaReading] = []
        for slot in ("primary", "secondary"):
            block = limits.get(slot)
            if not isinstance(block, Mapping):
                continue
            percent = block.get("used_percent")
            if isinstance(percent, bool) or not isinstance(percent, (int, float)):
                continue
            minutes = block.get("window_minutes")
            window = (
                FIVE_HOUR
                if isinstance(minutes, (int, float))
                and not isinstance(minutes, bool)
                and minutes <= SHORT_WINDOW_MAX_MINUTES
                else WEEKLY
            )
            used = _fraction(float(percent) / 100.0)
            reached = limits.get("rate_limit_reached_type")
            readings.append(
                QuotaReading(
                    provider=ref.provider,
                    account=ref.alias,
                    window=window,
                    source=self.name,
                    confidence=MEASURED,
                    observed_at_unix=observed,
                    used_fraction=used,
                    resets_at_unix=_unix(block.get("resets_at")),
                    exhausted=bool(reached) or (used is not None and used >= 1.0),
                    detail=(
                        f"{slot} window of {minutes} minutes, reported by Codex in "
                        f"{path.name}"
                    ),
                )
            )
        return readings


# What Claude Code writes into the transcript when the provider refuses. Three
# shapes appear in real records, and they mean different things:
#   "You've hit your session limit · resets 2:50am (America/Chicago)"
#   "You've hit your weekly limit · resets Jul 24, 11pm (America/Chicago)"
#   "You've reached your Fable 5 limit. Run /usage-credits to continue ..."
# The first two name the window that ran out and when it comes back; the third
# is one model's limit, not the account's, and must not take the account out.
LIMIT_RE = re.compile(
    r"hit your (?P<kind>session|weekly) limit\s*[\u00b7:-]\s*resets\s+"
    r"(?P<when>[^()]+?)\s*\((?P<zone>[A-Za-z_]+/[A-Za-z_+\-]+)\)",
    re.IGNORECASE,
)
MODEL_LIMIT_RE = re.compile(r"reached your (?P<model>[^.]+?) limit", re.IGNORECASE)
CLOCK_RE = re.compile(
    r"\A(?:(?P<month>[A-Za-z]{3})[a-z]*\s+(?P<day>\d{1,2}),\s*)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>am|pm)\Z",
    re.IGNORECASE,
)
MONTHS = {
    name: index
    for index, name in enumerate(
        (
            "jan",
            "feb",
            "mar",
            "apr",
            "may",
            "jun",
            "jul",
            "aug",
            "sep",
            "oct",
            "nov",
            "dec",
        ),
        start=1,
    )
}


def normalized_model(label: str) -> str:
    """Reduce a model name to letters and digits for matching a catalog entry."""
    return "".join(character for character in label.lower() if character.isalnum())


def parse_reset(when: str, zone: str, after: int) -> int | None:
    """Turn "2:50am (America/Chicago)" into the next such instant after a time.

    Returns None when the clock text or the zone cannot be resolved. A reset
    time is only worth reporting if it is real; guessing one would tell the
    operator an account is back when it is not.
    """
    match = CLOCK_RE.match(when.strip())
    if match is None:
        return None
    try:
        from zoneinfo import ZoneInfo

        info = ZoneInfo(zone)
    except Exception:
        return None
    hour = int(match.group("hour")) % 12
    if match.group("meridiem").lower() == "pm":
        hour += 12
    minute = int(match.group("minute") or 0)
    start = datetime.fromtimestamp(after, tz=info)
    month = match.group("month")
    if month:
        index = MONTHS.get(month.lower())
        if index is None:
            return None
        day = int(match.group("day"))
        for year in (start.year, start.year + 1):
            try:
                moment = start.replace(
                    year=year,
                    month=index,
                    day=day,
                    hour=hour,
                    minute=minute,
                    second=0,
                    microsecond=0,
                )
            except ValueError:
                continue
            if moment.timestamp() >= after:
                return int(moment.timestamp())
        return None
    moment = start.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if moment.timestamp() < after:
        moment = moment + timedelta(days=1)
    return int(moment.timestamp())


@dataclass(frozen=True)
class _Refusal:
    """One 429 the provider returned, with whatever it said about itself."""

    at_unix: int
    window: str | None = None
    resets_at_unix: int | None = None
    model_hint: str = ""

    def binding(self, now: int, last_success: int = 0) -> bool:
        """Is this refusal still in force?

        A billed turn after the refusal settles it outright: the account
        answered, so whatever ran out has come back. That test also makes the
        bounded tail scan sufficient for the case that matters — an account
        refused *right now* cannot have written anything since, so the refusal
        is the newest thing in its newest record.

        Failing that, a stated reset time decides. Failing that too, a refusal
        speaks only for its own window, and only until that window turns over.
        """
        if last_success > self.at_unix:
            return False
        if self.resets_at_unix is not None:
            return now < self.resets_at_unix
        span = WEEK_SECONDS if self.window == WEEKLY else FIVE_HOUR_SECONDS
        return now - self.at_unix <= span


def refusal_from_record(record: Mapping[str, Any], stamp: int) -> _Refusal | None:
    """Read a refusal, and what it admitted, out of one transcript record."""
    if record.get("error") != "rate_limit" and record.get("apiErrorStatus") != 429:
        return None
    message = record.get("message")
    text = ""
    if isinstance(message, Mapping):
        for block in message.get("content") or []:
            if isinstance(block, Mapping) and isinstance(block.get("text"), str):
                text += block["text"]
    window_match = LIMIT_RE.search(text)
    if window_match is not None:
        kind = window_match.group("kind").lower()
        window = FIVE_HOUR if kind == "session" else WEEKLY
        return _Refusal(
            at_unix=stamp,
            window=window,
            resets_at_unix=parse_reset(
                window_match.group("when"), window_match.group("zone"), stamp
            ),
        )
    model_match = MODEL_LIMIT_RE.search(text)
    if model_match is not None:
        return _Refusal(
            at_unix=stamp, model_hint=normalized_model(model_match.group("model"))
        )
    # A refusal the kit cannot read still happened. Treating it as the short
    # window is the conservative reading: it expires on its own.
    return _Refusal(at_unix=stamp, window=FIVE_HOUR)


class ClaudeTranscriptUsageSource:
    """Infer Claude window load from the transcripts Claude Code writes locally.

    Claude Code publishes no quota figure on disk, so there is no percentage to
    read — only evidence of what this account has already spent. Two things are
    genuinely knowable: the tokens billed inside each window, and whether the
    provider answered with a 429. The first is a relative load signal, useful
    for ranking one account against another; it is deliberately not converted
    into a fraction of an allowance nobody published. The second is decisive: a
    429 inside the short window means the account is out, whatever the token
    count says.
    """

    name = "claude-transcript-usage"

    def __init__(self, *, files: int | None = None, tail_bytes: int | None = None):
        environ = os.environ
        self.files = files or _positive_int(
            "SESSION_KIT_QUOTA_TRANSCRIPT_FILES", DEFAULT_TRANSCRIPT_FILES, environ
        )
        self.tail_bytes = tail_bytes or _positive_int(
            "SESSION_KIT_QUOTA_TAIL_BYTES", DEFAULT_TAIL_BYTES, environ
        )

    def read(self, ref: AccountRef, *, now: int) -> list[QuotaReading]:
        if ref.provider != "claude" or ref.home is None:
            return []
        projects = ref.home / "projects"
        if not projects.is_dir():
            return []
        horizon = now - WEEK_SECONDS
        candidates = [
            path
            for path in projects.rglob("*.jsonl")
            if _safe_regular_file(path) and self._recent(path, horizon)
        ]
        totals = {FIVE_HOUR: 0.0, WEEKLY: 0.0}
        turns = {FIVE_HOUR: 0, WEEKLY: 0}
        refusals: list[_Refusal] = []
        latest = 0
        oldest = 0
        last_success = 0
        for path in _newest(candidates, self.files):
            for line in _tail_lines(path, self.tail_bytes):
                if '"assistant"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("type") != "assistant":
                    continue
                stamp = _iso_to_unix(record.get("timestamp"))
                if stamp is None or stamp < horizon:
                    continue
                latest = max(latest, stamp)
                oldest = stamp if not oldest else min(oldest, stamp)
                refusal = refusal_from_record(record, stamp)
                if refusal is not None:
                    refusals.append(refusal)
                    continue
                tokens = self._tokens(record)
                if tokens <= 0:
                    continue
                last_success = max(last_success, stamp)
                totals[WEEKLY] += tokens
                turns[WEEKLY] += 1
                if stamp >= now - FIVE_HOUR_SECONDS:
                    totals[FIVE_HOUR] += tokens
                    turns[FIVE_HOUR] += 1
        if not latest:
            return []
        readings: list[QuotaReading] = []
        # The scan is bounded — the newest few files, read from the tail — so
        # the sample can start later than the window does. Saying "in the last
        # seven days" when the evidence only reaches back two would overstate
        # what was read, so every reading carries the span it actually covers.
        covered = max(0, now - oldest) if oldest else 0
        binding = [row for row in refusals if row.binding(now, last_success)]
        for window, seconds in ((FIVE_HOUR, FIVE_HOUR_SECONDS), (WEEKLY, WEEK_SECONDS)):
            refused = max(
                (row for row in binding if row.window == window),
                key=lambda row: row.at_unix,
                default=None,
            )
            exhausted = refused is not None
            span = max(MIN_COVERAGE_SECONDS, min(covered, seconds))
            # A rate, not a total. Two accounts' scans rarely reach back the
            # same distance — a busy account fills its tail in an hour while a
            # quiet one's covers days — so comparing raw totals would rank the
            # account with the longer sample as the busier one. Per-hour over
            # the span actually read is comparable; a total is not.
            rate = totals[window] / (span / 3600.0)
            detail = (
                f"{turns[window]} billed turn(s) over the {span / 3600:.1f}h this "
                f"scan reached back, inside a {seconds // 3600}h window; Claude "
                "publishes no local allowance, so this is relative load, not a "
                "percentage"
            )
            if span < seconds:
                detail += (
                    f" (bounded scan: newest {self.files} transcript file(s), "
                    f"{self.tail_bytes // 1024}KB tail each)"
                )
            if refused is not None:
                label = "session" if window == FIVE_HOUR else "weekly"
                when = (
                    f", resets at unix {refused.resets_at_unix}"
                    if refused.resets_at_unix
                    else ", with no reset time stated"
                )
                detail = (
                    f"provider refused the {label} limit at unix "
                    f"{refused.at_unix}{when}; " + detail
                )
            readings.append(
                QuotaReading(
                    provider=ref.provider,
                    account=ref.alias,
                    window=window,
                    source=self.name,
                    confidence=OBSERVED,
                    observed_at_unix=latest,
                    used_fraction=1.0 if exhausted else None,
                    resets_at_unix=refused.resets_at_unix if refused else None,
                    load_units=rate,
                    load_unit="tokens/h",
                    exhausted=exhausted,
                    detail=detail,
                )
            )
        # A single model's limit is that model's problem. Reporting it against
        # the account would strand every other model on a healthy account.
        for hint in sorted({row.model_hint for row in binding if row.model_hint}):
            readings.append(
                QuotaReading(
                    provider=ref.provider,
                    account=ref.alias,
                    window=FIVE_HOUR,
                    source=self.name,
                    confidence=OBSERVED,
                    observed_at_unix=latest,
                    used_fraction=1.0,
                    exhausted=True,
                    model_hint=hint,
                    detail=(
                        "the provider refused this model's own limit; the "
                        "account's other models are unaffected"
                    ),
                )
            )
        return readings

    @staticmethod
    def _recent(path: Path, horizon: int) -> bool:
        try:
            return path.stat().st_mtime >= horizon
        except OSError:
            return False

    @staticmethod
    def _tokens(record: Mapping[str, Any]) -> float:
        message = record.get("message")
        if not isinstance(message, Mapping):
            return 0.0
        usage = message.get("usage")
        if not isinstance(usage, Mapping):
            return 0.0
        total = 0.0
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            value = usage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                total += float(value)
        return total


class AccountFeedQuotaSource:
    """Read window fractions from the account roster the kit already consumes.

    This is the plug point decision 11 describes. The kit ships the reader, not
    the feed: with no roster configured it yields nothing and the local readers
    carry the decision. Where an operator has pointed the kit at an
    authoritative allowances feed, the same reader turns it into readings for
    both providers — the roster has always carried a ``codex_accounts`` list,
    so nothing here is Claude-only.

    The kit cannot verify what produced these numbers, so they are recorded as
    a feed assertion, ranked below a provider-published measurement and above
    the kit's own inference. A stale roster is dropped rather than trusted.
    """

    name = "account-feed"

    def __init__(self, config: Mapping[str, Any]):
        self.config = config
        self._cache: dict[tuple[str, int], Mapping[str, Any] | None] = {}

    def _choices(self, provider: str, now: int) -> Mapping[str, Any] | None:
        """One roster read per provider per pass, not one per account.

        Every account of a provider answers from the same roster file. Keying
        the memo on the pass timestamp keeps a later pass from reusing a stale
        answer, which matters because roster freshness is the whole point of
        reading it.
        """
        key = (provider, now)
        if key not in self._cache:
            module = _accounts_module()
            try:
                self._cache[key] = (
                    None
                    if module is None
                    else module.account_choices(self.config, provider)
                )
            except Exception:
                self._cache[key] = None
        return self._cache[key]

    def read(self, ref: AccountRef, *, now: int) -> list[QuotaReading]:
        payload = self._choices(ref.provider, now)
        if payload is None:
            return []
        if not payload.get("roster_fresh"):
            return []
        row = next(
            (
                item
                for item in payload.get("choices", [])
                if item.get("alias") == ref.alias
            ),
            None,
        )
        if row is None:
            return []
        readings: list[QuotaReading] = []
        for window, key in ((FIVE_HOUR, "u5h"), (WEEKLY, "u7d")):
            used = _fraction(row.get(key))
            if used is None:
                continue
            readings.append(
                QuotaReading(
                    provider=ref.provider,
                    account=ref.alias,
                    window=window,
                    source=self.name,
                    confidence=FEED,
                    observed_at_unix=now,
                    used_fraction=used,
                    exhausted=used >= 1.0 or row.get("serving") is False,
                    detail=(
                        f"roster reports {used * 100:.0f}% of the {window} window "
                        f"used; health {row.get('health') or 'unverified'}"
                    ),
                )
            )
        return readings


SourceFactory = Callable[[Mapping[str, Any]], Any]

_EXTRA_SOURCES: list[SourceFactory] = []


def register_source(factory: SourceFactory) -> None:
    """Add a quota reader the public kit does not ship.

    A deployment with a richer signal — an authoritative allowances feed, a
    provider API poller — registers it here instead of editing this module, so
    one selection equation runs everywhere and only its inputs differ.
    """
    _EXTRA_SOURCES.append(factory)


def clear_registered_sources() -> None:
    """Drop every registered source. For a caller that owns the process."""
    _EXTRA_SOURCES.clear()


def default_sources(config: Mapping[str, Any]) -> list[Any]:
    """Build the shipped local readers plus anything a deployment registered."""
    sources: list[Any] = [
        CodexRateLimitSource(),
        ClaudeTranscriptUsageSource(),
        AccountFeedQuotaSource(config),
    ]
    sources.extend(factory(config) for factory in _EXTRA_SOURCES)
    selected = os.environ.get("SESSION_KIT_QUOTA_SOURCES", "").strip()
    if not selected:
        return sources
    wanted = {name.strip() for name in selected.split(",") if name.strip()}
    return [source for source in sources if getattr(source, "name", "") in wanted]


@dataclass
class QuotaSnapshot:
    """Every reading collected for one pass, indexed for the scorer."""

    taken_at_unix: int
    readings: list[QuotaReading] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS

    def for_account(self, provider: str, alias: str) -> list[QuotaReading]:
        return [
            reading
            for reading in self.readings
            if reading.provider == provider and reading.account == alias
        ]

    def model_scoped(self, provider: str, alias: str) -> list[QuotaReading]:
        """Readings that speak for one model rather than for the account."""
        return [
            reading
            for reading in self.for_account(provider, alias)
            if reading.model_hint
        ]

    def best(self, provider: str, alias: str, window: str) -> QuotaReading | None:
        """Return the strongest fresh reading for one window.

        Strength is provenance first — a provider's own number beats a feed's,
        which beats the kit's inference — then recency. A reading older than
        the staleness bound is only used when nothing fresher exists, and the
        caller can still see its age.
        """
        # Account-wide only. A reading scoped to one model says nothing about
        # what the account has left for every other model, so folding it in
        # here would take a healthy account's headroom to zero.
        rows = [
            reading
            for reading in self.for_account(provider, alias)
            if reading.window == window and not reading.model_hint
        ]
        if not rows:
            return None
        rows.sort(
            key=lambda reading: (
                reading.stale(self.taken_at_unix, self.max_age_seconds),
                CONFIDENCE_ORDER.index(reading.confidence),
                -reading.observed_at_unix,
            )
        )
        return rows[0]

    def as_dict(self) -> dict[str, Any]:
        return {
            "taken_at_unix": self.taken_at_unix,
            "max_age_seconds": self.max_age_seconds,
            "readings": [reading.as_dict() for reading in self.readings],
            "errors": list(self.errors),
        }


def collect(
    config: Mapping[str, Any],
    refs: Sequence[AccountRef],
    *,
    sources: Sequence[Any] | None = None,
    now: int | None = None,
) -> QuotaSnapshot:
    """Ask every source about every account; a broken source is not fatal.

    One unreadable transcript directory must not cost the whole proposal. A
    source that raises is recorded as an error the proposal surface prints, and
    the remaining sources still answer.
    """
    stamp = _now(now)
    readers = list(sources) if sources is not None else default_sources(config)
    snapshot = QuotaSnapshot(
        taken_at_unix=stamp,
        max_age_seconds=_positive_int(
            "SESSION_KIT_QUOTA_MAX_AGE_SECONDS", DEFAULT_MAX_AGE_SECONDS, os.environ
        ),
    )
    for ref in refs:
        for reader in readers:
            try:
                snapshot.readings.extend(reader.read(ref, now=stamp))
            except Exception as exc:  # one bad reader must not sink the pass
                snapshot.errors.append(
                    f"{getattr(reader, 'name', 'source')} failed for {ref.key}: {exc}"
                )
    return snapshot
