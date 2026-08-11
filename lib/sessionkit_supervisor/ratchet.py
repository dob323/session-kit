"""Persistent authority lanes, outcome streaks, and autonomous-turn budgets.

This module deliberately has no dependency on the supervisor adapter or its
vendored implementation.  The resident session can therefore enforce its
authority policy even when the optional MCP surface is unavailable.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence


OUTCOMES = frozenset(("ok", "bad", "unknown"))
PROMOTION_STREAK = 20
MAX_FILE_BYTES = 2 * 1024 * 1024


class RatchetError(ValueError):
    """The ratchet state or a requested transition is invalid."""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _utc_day(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).date().isoformat()


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_uid != os.geteuid()
    ):
        raise RatchetError(
            f"supervisor state must be a mode-0700 current-owner directory: {path}"
        )


def _read_json(path: Path) -> Any:
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise RatchetError(f"required supervisor file does not exist: {path}") from None
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise RatchetError(f"supervisor file must be a regular file: {path}")
    if info.st_size > MAX_FILE_BYTES:
        raise RatchetError(f"supervisor file exceeds {MAX_FILE_BYTES} bytes: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise RatchetError(f"supervisor file is not valid JSON: {path}") from exc


def _atomic_private_json(path: Path, value: Any) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _valid_category(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 64
        or not value.replace("_", "a").isalnum()
        or value[0].isdigit()
    ):
        raise RatchetError(f"invalid supervisor category: {value!r}")
    return value


def _validate_lanes(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping) or not value:
        raise RatchetError("lanes document must be a non-empty object")
    checked: dict[str, dict[str, Any]] = {}
    for raw_category, raw_entry in value.items():
        category = _valid_category(raw_category)
        if not isinstance(raw_entry, Mapping) or set(raw_entry) != {
            "lane",
            "streak",
            "promoted_at",
        }:
            raise RatchetError(f"invalid lane entry for {category}")
        lane = raw_entry.get("lane")
        streak = raw_entry.get("streak")
        promoted_at = raw_entry.get("promoted_at")
        if lane not in (1, 2, 3) or isinstance(lane, bool):
            raise RatchetError(f"invalid authority lane for {category}")
        if not isinstance(streak, int) or isinstance(streak, bool) or streak < 0:
            raise RatchetError(f"invalid outcome streak for {category}")
        if promoted_at is not None and (
            not isinstance(promoted_at, int)
            or isinstance(promoted_at, bool)
            or promoted_at <= 0
        ):
            raise RatchetError(f"invalid promotion timestamp for {category}")
        checked[category] = {
            "lane": lane,
            "streak": streak,
            "promoted_at": promoted_at,
        }
    return checked


def _validate_budgets(value: Any) -> dict[str, int | float]:
    if not isinstance(value, Mapping) or set(value) != {
        "per_day_autonomous_turns",
        "per_day_usd_est",
    }:
        raise RatchetError("budgets document has an invalid shape")
    turns = value.get("per_day_autonomous_turns")
    usd = value.get("per_day_usd_est")
    if not isinstance(turns, int) or isinstance(turns, bool) or turns <= 0:
        raise RatchetError("per_day_autonomous_turns must be a positive integer")
    if not isinstance(usd, (int, float)) or isinstance(usd, bool) or usd <= 0:
        raise RatchetError("per_day_usd_est must be positive")
    return {"per_day_autonomous_turns": turns, "per_day_usd_est": float(usd)}


class Ratchet:
    """Own the private, append-only authority ledger under one state directory."""

    def __init__(
        self,
        state_dir: Path,
        *,
        config_dir: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.root = self.state_dir / "supervisor"
        self.config_dir = (
            config_dir
            or Path(__file__).resolve().parents[2] / "config" / "supervisor"
        )
        self.lanes_path = self.root / "lanes.json"
        self.budgets_path = self.root / "budgets.json"
        self.ledger_path = self.root / "ledger.jsonl"
        self.rotated_ledger_path = self.root / "ledger.jsonl.1"
        self.claims_path = self.root / "budget-claims.json"
        self.lock_path = self.root / "ratchet.lock"
        self.clock = clock

    def ensure(self) -> None:
        """Create private runtime copies of the source-controlled seeds."""
        _private_directory(self.root)
        self._ensure_seed(self.lanes_path, self.config_dir / "lanes.json", _validate_lanes)
        self._ensure_seed(
            self.budgets_path,
            self.config_dir / "budgets.json",
            _validate_budgets,
        )
        if not self.ledger_path.exists():
            try:
                descriptor = os.open(
                    self.ledger_path,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
        self._check_private_file(self.ledger_path)
        if self.rotated_ledger_path.exists() or self.rotated_ledger_path.is_symlink():
            self._check_private_file(self.rotated_ledger_path)
        if self.claims_path.exists() or self.claims_path.is_symlink():
            self._check_private_file(self.claims_path)

    def _ensure_seed(
        self,
        destination: Path,
        seed: Path,
        validator: Callable[[Any], Any],
    ) -> None:
        if destination.is_symlink():
            raise RatchetError(f"supervisor file must not be a symlink: {destination}")
        if destination.exists():
            self._check_private_file(destination)
            validator(_read_json(destination))
            return
        value = validator(_read_json(seed))
        _atomic_private_json(destination, value)

    @staticmethod
    def _check_private_file(path: Path) -> None:
        info = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.geteuid()
        ):
            raise RatchetError(
                f"supervisor state must be a mode-0600 current-owner file: {path}"
            )

    @contextlib.contextmanager
    def _locked(self) -> Any:
        self.ensure()
        descriptor = os.open(
            self.lock_path,
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or stat.S_IMODE(opened.st_mode) != 0o600
                or opened.st_uid != os.geteuid()
            ):
                raise RatchetError("ratchet lock is not a private regular file")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def lanes(self) -> dict[str, dict[str, Any]]:
        self.ensure()
        return _validate_lanes(_read_json(self.lanes_path))

    def budgets(self) -> dict[str, int | float]:
        self.ensure()
        return _validate_budgets(_read_json(self.budgets_path))

    def _rotate_ledger_if_needed(self, incoming_bytes: int = 0) -> bool:
        """Keep one private rotation before the active ledger reaches its bound."""
        self._check_private_file(self.ledger_path)
        if self.ledger_path.stat().st_size + incoming_bytes < MAX_FILE_BYTES:
            return False
        if self.rotated_ledger_path.exists() or self.rotated_ledger_path.is_symlink():
            self._check_private_file(self.rotated_ledger_path)
            if self.rotated_ledger_path.stat().st_size > 0:
                # The single-rotation policy is about to discard history.
                # Count it durably so budget_status can say so — silence here
                # would falsify the append-only story.
                self._note_rotation_discard()
        os.replace(self.ledger_path, self.rotated_ledger_path)
        os.chmod(self.rotated_ledger_path, 0o600)
        descriptor = os.open(
            self.ledger_path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return True

    def _note_rotation_discard(self) -> None:
        path = self.root / "rotation-discards.json"
        count = 0
        if path.exists():
            self._check_private_file(path)
            value = _read_json(path)
            if isinstance(value, Mapping):
                raw = value.get("discarded_rotations")
                if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
                    count = raw
        _atomic_private_json(path, {"discarded_rotations": count + 1})

    def _discarded_rotations(self) -> int:
        path = self.root / "rotation-discards.json"
        if not path.exists():
            return 0
        self._check_private_file(path)
        value = _read_json(path)
        if isinstance(value, Mapping):
            raw = value.get("discarded_rotations")
            if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
                return raw
        return 0

    def _read_ledger_file(self, path: Path) -> tuple[list[dict[str, Any]], int]:
        self._check_private_file(path)
        rows: list[dict[str, Any]] = []
        malformed = 0
        try:
            handle = path.open("rb")
        except OSError as exc:
            raise RatchetError("cannot read supervisor ledger") from exc
        with handle:
            for raw_line in handle:
                try:
                    row = json.loads(raw_line.decode("utf-8"))
                except (UnicodeError, ValueError):
                    malformed += 1
                    continue
                if not isinstance(row, Mapping):
                    malformed += 1
                    continue
                rows.append(dict(row))
        return rows, malformed

    def _ledger(self) -> tuple[list[dict[str, Any]], int]:
        self._rotate_ledger_if_needed()
        rows: list[dict[str, Any]] = []
        malformed = 0
        if self.rotated_ledger_path.exists():
            rotated_rows, rotated_malformed = self._read_ledger_file(
                self.rotated_ledger_path
            )
            rows.extend(rotated_rows)
            malformed += rotated_malformed
        active_rows, active_malformed = self._read_ledger_file(self.ledger_path)
        rows.extend(active_rows)
        malformed += active_malformed
        return rows, malformed

    def _claims(self, day: str) -> list[dict[str, Any]]:
        if not self.claims_path.exists():
            return []
        self._check_private_file(self.claims_path)
        value = _read_json(self.claims_path)
        if not isinstance(value, list):
            raise RatchetError("budget claims document must be an array")
        claims: list[dict[str, Any]] = []
        for raw in value:
            pid = raw.get("pid") if isinstance(raw, Mapping) else None
            usd_est = raw.get("usd_est") if isinstance(raw, Mapping) else None
            if (
                not isinstance(raw, Mapping)
                or raw.get("day_utc") != day
                or not isinstance(pid, int)
                or isinstance(pid, bool)
                or pid <= 0
                or not isinstance(raw.get("category"), str)
                or not isinstance(usd_est, (int, float))
                or isinstance(usd_est, bool)
                or usd_est < 0
                or not _pid_alive(pid)
            ):
                # A claim whose process died between authorize and record
                # would otherwise hold its budget slot for the rest of the
                # UTC day. Dropping it here rewrites the pruned file below.
                continue
            claims.append(dict(raw))
        if len(claims) != len(value):
            _atomic_private_json(self.claims_path, claims)
        return claims

    def _budget_status_unlocked(
        self,
        *,
        now_ms: int | None,
        usd_est_today: float | None,
    ) -> dict[str, Any]:
        stamp = now_ms if now_ms is not None else int(self.clock() * 1000)
        day = _utc_day(stamp)
        rows, malformed = self._ledger()
        claims = self._claims(day)
        recorded_turns = sum(
            1
            for row in rows
            if isinstance(row.get("ts"), int)
            and not isinstance(row.get("ts"), bool)
            and _utc_day(row["ts"]) == day
            and row.get("lane") in (1, 2)
            and row.get("operator_confirmed") is not True
        )
        turns = recorded_turns + len(claims)
        limits = _validate_budgets(_read_json(self.budgets_path))
        inferred = usd_est_today is None
        claimed_usd = sum(float(claim["usd_est"]) for claim in claims)
        if inferred:
            usd_est_today = recorded_turns * (
                float(limits["per_day_usd_est"])
                / int(limits["per_day_autonomous_turns"])
            )
        if (
            not isinstance(usd_est_today, (int, float))
            or isinstance(usd_est_today, bool)
            or usd_est_today < 0
        ):
            raise RatchetError("usd_est_today must be a non-negative number")
        effective_usd = float(usd_est_today) + claimed_usd
        turn_limit = int(limits["per_day_autonomous_turns"])
        usd_limit = float(limits["per_day_usd_est"])
        reasons: list[str] = []
        if turns >= turn_limit:
            reasons.append("per_day_autonomous_turns")
        if effective_usd >= usd_limit:
            reasons.append("per_day_usd_est")
        warnings = []
        if malformed:
            noun = "line" if malformed == 1 else "lines"
            warnings.append(f"skipped {malformed} malformed supervisor ledger {noun}")
        discarded = self._discarded_rotations()
        if discarded:
            noun = "rotation" if discarded == 1 else "rotations"
            warnings.append(
                f"{discarded} earlier supervisor ledger {noun} discarded by the"
                " single-rotation bound"
            )
        return {
            "day_utc": day,
            "autonomous_turns": turns,
            "per_day_autonomous_turns": turn_limit,
            "usd_est": round(effective_usd, 6),
            "usd_est_inferred": inferred,
            "per_day_usd_est": usd_limit,
            "exceeded": bool(reasons),
            "reasons": reasons,
            "warnings": warnings,
        }

    def budget_status(
        self,
        *,
        now_ms: int | None = None,
        usd_est_today: float | None = None,
    ) -> dict[str, Any]:
        """Return today's usage; infer a conservative USD estimate if omitted."""
        with self._locked():
            return self._budget_status_unlocked(
                now_ms=now_ms,
                usd_est_today=usd_est_today,
            )

    def authorize(
        self,
        category: str,
        *,
        now_ms: int | None = None,
        usd_est_today: float | None = None,
        next_usd_est: float | None = None,
        operator_confirmed: bool = False,
    ) -> dict[str, Any]:
        """Return a skip-not-abort decision for one proposed autonomous act."""
        category = _valid_category(category)
        if not isinstance(operator_confirmed, bool):
            raise RatchetError("operator_confirmed must be a boolean")
        with self._locked():
            lanes = _validate_lanes(_read_json(self.lanes_path))
            if category not in lanes:
                raise RatchetError(f"unknown supervisor category: {category}")
            lane = lanes[category]["lane"]
            budget = self._budget_status_unlocked(
                now_ms=now_ms,
                usd_est_today=usd_est_today,
            )
            # Budgets bound AUTONOMOUS acts. An act the operator explicitly
            # confirmed is not autonomous: it consumes no claim, so it is
            # denied by no budget — only validated.
            reasons = [] if operator_confirmed else list(budget["reasons"])
            if lane == 3 and not operator_confirmed:
                reasons.append("lane3_requires_operator")
            if next_usd_est is not None:
                if (
                    not isinstance(next_usd_est, (int, float))
                    or isinstance(next_usd_est, bool)
                    or next_usd_est < 0
                ):
                    raise RatchetError("next_usd_est must be a non-negative number")
                if not operator_confirmed and (
                    budget["usd_est"] + float(next_usd_est) > budget["per_day_usd_est"]
                ):
                    reasons.append("per_day_usd_est")
            reasons = list(dict.fromkeys(reasons))
            if not reasons and not operator_confirmed:
                limits = _validate_budgets(_read_json(self.budgets_path))
                claim_usd = (
                    float(next_usd_est)
                    if next_usd_est is not None
                    else float(limits["per_day_usd_est"])
                    / int(limits["per_day_autonomous_turns"])
                )
                claims = self._claims(budget["day_utc"])
                claims.append(
                    {
                        "day_utc": budget["day_utc"],
                        "pid": os.getpid(),
                        "category": category,
                        "usd_est": claim_usd,
                    }
                )
                _atomic_private_json(self.claims_path, claims)
            return {
                "allowed": not reasons,
                "skip_not_abort": bool(reasons),
                "reason": (
                    "lane"
                    if "lane3_requires_operator" in reasons
                    else "budget" if reasons else None
                ),
                "category": category,
                "lane": lane,
                "reasons": reasons,
                "budget": budget,
            }

    def record(
        self,
        *,
        category: str,
        action: str,
        target: str,
        outcome: str,
        brief_shown: bool,
        lane: int | None = None,
        ts: int | None = None,
        operator_confirmed: bool = False,
        authority_event_id: str | None = None,
        authority_scope: str | None = None,
        authority_request_id: str | None = None,
    ) -> dict[str, Any]:
        """Append one act and apply its streak/demotion transition."""
        category = _valid_category(category)
        if outcome not in OUTCOMES:
            raise RatchetError(f"invalid supervisor outcome: {outcome!r}")
        if not isinstance(brief_shown, bool):
            raise RatchetError("brief_shown must be a boolean")
        if not isinstance(operator_confirmed, bool):
            raise RatchetError("operator_confirmed must be a boolean")
        confirmed_scope = ""
        if operator_confirmed:
            if (
                not isinstance(authority_event_id, str)
                or not re.fullmatch(r"[0-9a-f]{64}", authority_event_id)
            ):
                raise RatchetError("a confirmed act needs an exact authority_event_id")
            if (
                not isinstance(authority_request_id, str)
                or not re.fullmatch(r"[0-9a-f]{64}", authority_request_id)
            ):
                raise RatchetError("a confirmed act needs an exact authority_request_id")
            if (
                not isinstance(authority_scope, str)
                or not authority_scope.strip()
                or len(authority_scope) > 1000
            ):
                raise RatchetError("a confirmed act needs a bounded authority scope")
            # Bind the proved value here. The row below is written 60 lines
            # later, past branches that would let a reader — or a type checker
            # — lose track of the guarantee this branch just established.
            confirmed_scope = authority_scope.strip()
        for label, value in (("action", action), ("target", target)):
            if not isinstance(value, str) or not value.strip() or len(value) > 1000:
                raise RatchetError(f"{label} must be 1-1000 characters")
        stamp = ts if ts is not None else int(self.clock() * 1000)
        if not isinstance(stamp, int) or isinstance(stamp, bool) or stamp <= 0:
            raise RatchetError("ts must be a positive Unix-millisecond integer")
        with self._locked():
            lanes = _validate_lanes(_read_json(self.lanes_path))
            if category not in lanes:
                raise RatchetError(f"unknown supervisor category: {category}")
            configured_lane = lanes[category]["lane"]
            actual_lane = configured_lane if lane is None else lane
            # The row records the lane the act RAN under. Gates can only
            # tighten, so the effective lane may exceed the seed lane — a
            # confirmed broadcast in a lane-2 category runs at 3 — but an act
            # may never claim a looser lane than its category is configured.
            if (
                isinstance(actual_lane, bool)
                or actual_lane not in (1, 2, 3)
                or actual_lane < configured_lane
            ):
                raise RatchetError(
                    f"act lane {actual_lane!r} is looser than {category} lane {configured_lane}"
                )
            if actual_lane == 3 and not operator_confirmed:
                raise RatchetError(
                    "Lane 3 acts require the operator and are not autonomous acts"
                )
            if outcome == "bad":
                lanes[category].update(lane=3, streak=0)
            elif outcome == "ok" and configured_lane == 2 and not operator_confirmed:
                # Only genuinely autonomous clean acts build the promotion
                # streak — an operator-confirmed act proves the operator's
                # judgment, not the supervisor's.
                lanes[category]["streak"] += 1
            elif configured_lane == 2:
                lanes[category]["streak"] = 0
            else:
                lanes[category]["streak"] = 0
            row = {
                "ts": stamp,
                "lane": actual_lane,
                "operator_confirmed": operator_confirmed,
                "category": category,
                "action": action.strip(),
                "target": target.strip(),
                "outcome": outcome,
                "brief_shown": brief_shown,
            }
            if operator_confirmed:
                row["authority_event_id"] = authority_event_id
                row["authority_request_id"] = authority_request_id
                row["authority_scope"] = confirmed_scope
            encoded = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode(
                "utf-8"
            )
            self._rotate_ledger_if_needed(len(encoded))
            descriptor = os.open(
                self.ledger_path,
                os.O_WRONLY
                | os.O_APPEND
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                if os.write(descriptor, encoded) != len(encoded):
                    raise RatchetError("short write to supervisor ledger")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            claim_day = _utc_day(stamp)
            claims = self._claims(claim_day)
            for index, claim in enumerate(claims):
                if claim["pid"] == os.getpid() and claim["category"] == category:
                    del claims[index]
                    _atomic_private_json(self.claims_path, claims)
                    break
            _atomic_private_json(self.lanes_path, lanes)
            eligible = (
                lanes[category]["lane"] == 2
                and lanes[category]["streak"] >= PROMOTION_STREAK
            )
            return {
                "recorded": True,
                "entry": row,
                "lane": lanes[category]["lane"],
                "streak": lanes[category]["streak"],
                "eligible": eligible,
                "demoted": outcome == "bad",
            }

    def mark_briefed(self, timestamps: Sequence[int]) -> dict[str, Any]:
        """Flip brief_shown on the named rows — the one sanctioned rewrite.

        The ledger is append-only for CONTENT; surfacing is the single field
        whose truth only exists after the row was written. The flip happens
        under the same lock as every write, changes nothing but that boolean,
        and never touches the rotated file — an act that rotated away unshown
        stays honestly unshown.
        """
        wanted: set[int] = set()
        for value in timestamps:
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise RatchetError("timestamps must be positive Unix-millisecond integers")
            wanted.add(value)
        if not wanted:
            raise RatchetError("at least one timestamp is required")
        with self._locked():
            rows, _ = self._read_ledger_file(self.ledger_path)
            flipped = 0
            for row in rows:
                if row.get("ts") in wanted and row.get("brief_shown") is not True:
                    row["brief_shown"] = True
                    flipped += 1
            if flipped:
                encoded = b"".join(
                    (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
                    for row in rows
                )
                temporary = self.ledger_path.with_name(".ledger.briefed")
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                )
                try:
                    if os.write(descriptor, encoded) != len(encoded):
                        raise RatchetError("short write while marking rows briefed")
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                os.replace(temporary, self.ledger_path)
            return {"flipped": flipped, "requested": len(wanted)}

    def promote(
        self,
        category: str,
        *,
        operator_confirmed: bool,
        ts: int | None = None,
    ) -> dict[str, Any]:
        """Apply exactly one human-confirmed promotion; never auto-promote."""
        category = _valid_category(category)
        if operator_confirmed is not True:
            raise RatchetError("promotion requires the operator's explicit confirmation")
        stamp = ts if ts is not None else int(self.clock() * 1000)
        if not isinstance(stamp, int) or isinstance(stamp, bool) or stamp <= 0:
            raise RatchetError("ts must be a positive Unix-millisecond integer")
        with self._locked():
            lanes = _validate_lanes(_read_json(self.lanes_path))
            if category not in lanes:
                raise RatchetError(f"unknown supervisor category: {category}")
            current = lanes[category]["lane"]
            if current == 1:
                raise RatchetError(f"{category} is already Lane 1")
            if current == 2 and lanes[category]["streak"] < PROMOTION_STREAK:
                raise RatchetError(
                    f"{category} needs {PROMOTION_STREAK} clean Lane 2 acts before promotion"
                )
            lanes[category] = {
                "lane": current - 1,
                "streak": 0,
                "promoted_at": stamp,
            }
            _atomic_private_json(self.lanes_path, lanes)
            return dict(lanes[category])


def record_act(state_dir: Path, **kwargs: Any) -> dict[str, Any]:
    """Small functional facade for adapters that do not retain a Ratchet."""
    return Ratchet(state_dir).record(**kwargs)


def authorize_act(state_dir: Path, category: str, **kwargs: Any) -> dict[str, Any]:
    """Small functional facade returning the skip-not-abort decision."""
    return Ratchet(state_dir).authorize(category, **kwargs)
