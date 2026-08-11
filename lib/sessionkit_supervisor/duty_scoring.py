"""Score every (provider, model, account) a duty could run on.

One equation runs everywhere; only its inputs get richer. A duty scores each
placement from four terms:

* **fit** — how well the model's policy strength matches the kind of work.
* **weekly headroom** — how much of the seven-day window the account has left.
* **window** — how much of the short rolling window it has left, and how soon
  that window resets.
* **cost** — a light preference for the cheaper placement among equals, so
  mechanical work does not consume the deepest model by default.

Two rules keep the output honest. A term whose input is unknown does not
quietly become a good number: it falls back to a neutral prior and the basis is
recorded as ``unknown`` so the proposal can print it. And where no provider
publishes a percentage — Claude Code does not, locally — accounts are still
comparable by *relative* load against each other, recorded as ``relative-load``
and never dressed up as a percentage of an allowance nobody stated.

Skipped placements are scored and kept, not dropped. Decision 10 asks the plan
to show chosen *and* skipped accounts with their numbers, which is only
possible if an excluded candidate still carries its readings and its reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from typing import Any, Mapping, Sequence

from . import quota_sources as qs
from .quota_sources import AccountRef, QuotaReading, QuotaSnapshot


# The kind of work a duty is. Decision 10 ties this to model choice:
# design/debug-heavy work wants the deep-reasoning model, mechanical work wants
# a cheaper one.
WORK_TYPES = (
    "design",
    "security",
    "debug",
    "research",
    "review",
    "mechanical",
    "docs",
)

# Desired policy strength per work type, on the same 1-4 scale as the catalog.
# This is operator policy, not a benchmark result.
DESIRED_STRENGTH: dict[str, int] = {
    "design": 4,
    "security": 4,
    "debug": 4,
    "research": 3,
    "review": 3,
    "mechanical": 2,
    "docs": 2,
}

STRENGTH_SPAN = 3  # widest possible distance on the 1-4 scale
# Fit falls away faster than linearly with distance. Decision 10 states the
# model preference per kind of work; a spare account should let quota pick
# among models that suit the work, not talk a design duty onto a weaker model
# because some other account happens to be idle. Raising this makes the stated
# preference harder to outbid; 1.0 makes fit trade off linearly against quota.
FIT_CURVE = 2.0
UNKNOWN_PRIOR = 0.5
EXHAUSTION_FRACTION = 0.98
# A short window this close to resetting is nearly free again, so a nearly
# spent account is not written off for the sake of a few minutes.
IMMINENT_RESET_SECONDS = 20 * 60
IMMINENT_RESET_FLOOR = 0.35

DEFAULT_WEIGHTS: dict[str, float] = {
    "fit": 0.45,
    "weekly": 0.25,
    "window": 0.20,
    "cost": 0.10,
}


class ScoringError(ValueError):
    pass


@dataclass(frozen=True)
class ModelOption:
    """A model a duty can be placed on, with its policy ranking.

    ``strength`` and ``cost`` are policy dials on a 1-4 scale, not measured
    quality or price. The defaults encode decision 10 of the overhaul plan
    (design/debug-heavy work to the deep-reasoning model, mechanical work to a
    cheaper one); an operator who disagrees replaces the whole catalog through
    ``SESSION_KIT_MODEL_CATALOG`` rather than arguing with a hardcoded opinion.
    """

    provider: str
    model: str
    model_id: str
    strength: int
    cost: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "model_id": self.model_id,
            "strength": self.strength,
            "cost": self.cost,
        }


# Model identifiers observed in this machine's own provider records. A catalog
# entry is a name plus a policy ranking; nothing here claims a benchmark.
DEFAULT_CATALOG: tuple[ModelOption, ...] = (
    ModelOption("claude", "fable", "claude-fable-5", strength=4, cost=4),
    ModelOption("claude", "opus", "claude-opus-5", strength=3, cost=3),
    ModelOption("claude", "sonnet", "claude-sonnet-5", strength=2, cost=2),
    ModelOption("claude", "haiku", "claude-haiku-4-5-20251001", strength=1, cost=1),
    ModelOption("codex", "gpt-5.6-sol", "gpt-5.6-sol", strength=3, cost=2),
    ModelOption("codex", "gpt-5.6-terra", "gpt-5.6-terra", strength=3, cost=2),
)


def load_catalog(environ: Mapping[str, str] | None = None) -> list[ModelOption]:
    """Return the model catalog, replaced wholesale by an operator file if set.

    ``SESSION_KIT_MODEL_CATALOG`` points at a JSON list of catalog entries. A
    malformed file is refused rather than half-applied: a selection engine that
    silently falls back to defaults would place work on a model the operator
    thought they had removed.
    """
    values = environ if environ is not None else os.environ
    path = values.get("SESSION_KIT_MODEL_CATALOG", "").strip()
    if not path:
        return list(DEFAULT_CATALOG)
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ScoringError(f"model catalog is unreadable: {exc}") from exc
    if not isinstance(payload, list) or not payload:
        raise ScoringError("model catalog must be a non-empty list")
    catalog: list[ModelOption] = []
    for row in payload:
        if not isinstance(row, Mapping):
            raise ScoringError("model catalog entry must be an object")
        try:
            option = ModelOption(
                provider=str(row["provider"]),
                model=str(row["model"]),
                model_id=str(row.get("model_id") or row["model"]),
                strength=int(row["strength"]),
                cost=int(row["cost"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ScoringError(f"model catalog entry is invalid: {exc}") from exc
        if not 1 <= option.strength <= 4 or not 1 <= option.cost <= 4:
            raise ScoringError("model catalog strength and cost must be 1-4")
        catalog.append(option)
    return catalog


def load_weights(environ: Mapping[str, str] | None = None) -> dict[str, float]:
    """Return the term weights, overridable as a JSON object of the four keys."""
    values = environ if environ is not None else os.environ
    raw = values.get("SESSION_KIT_DUTY_WEIGHTS", "").strip()
    if not raw:
        return dict(DEFAULT_WEIGHTS)
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ScoringError(f"duty weights are not valid JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ScoringError("duty weights must be an object")
    weights = dict(DEFAULT_WEIGHTS)
    for key, value in payload.items():
        if key not in DEFAULT_WEIGHTS:
            raise ScoringError(f"unknown duty weight: {key}")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ScoringError(f"duty weight must be a non-negative number: {key}")
        weights[key] = float(value)
    if sum(weights.values()) <= 0:
        raise ScoringError("duty weights cannot all be zero")
    return weights


@dataclass(frozen=True)
class Duty:
    """One unit of delegated work, described well enough to place it."""

    duty_id: str
    title: str
    work_type: str

    def __post_init__(self) -> None:
        if not self.duty_id:
            raise ScoringError("duty id is required")
        if self.work_type not in WORK_TYPES:
            raise ScoringError(
                f"unknown work type {self.work_type!r}; expected one of "
                + ", ".join(WORK_TYPES)
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "duty_id": self.duty_id,
            "title": self.title,
            "work_type": self.work_type,
        }


@dataclass(frozen=True)
class Term:
    """One scored input, kept with everything needed to explain it.

    ``value`` is what the score used; ``raw_value`` is what was read before any
    scoring adjustment. They differ when a rule such as the imminent-reset
    floor lifts a nearly spent window. Keeping both means the proposal can
    print the measurement and the adjustment separately, instead of showing an
    adjusted figure with a provider's name on it.
    """

    name: str
    value: float
    weight: float
    basis: str
    detail: str
    raw_value: float | None = None

    @property
    def contribution(self) -> float:
        return self.value * self.weight

    @property
    def adjusted(self) -> bool:
        return self.raw_value is not None and abs(self.raw_value - self.value) > 1e-9

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": round(self.value, 4),
            "raw_value": None if self.raw_value is None else round(self.raw_value, 4),
            "weight": self.weight,
            "contribution": round(self.contribution, 4),
            "basis": self.basis,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Placement:
    """A scored (provider, model, account) option for one duty."""

    duty_id: str
    provider: str
    model: str
    model_id: str
    account: str
    email: str
    score: float
    terms: tuple[Term, ...]
    readings: tuple[QuotaReading, ...] = ()
    excluded: bool = False
    exclusion_reason: str = ""

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.model}/{self.account}"

    def rationale(self) -> str:
        if self.excluded:
            return f"skipped — {self.exclusion_reason}"
        parts = [
            f"{term.name} {term.value:.2f}×{term.weight:g}={term.contribution:.3f}"
            f" ({term.basis})"
            for term in self.terms
        ]
        return f"score {self.score:.3f} = " + " + ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "duty_id": self.duty_id,
            "provider": self.provider,
            "model": self.model,
            "model_id": self.model_id,
            "account": self.account,
            "email": self.email,
            "score": round(self.score, 4),
            "excluded": self.excluded,
            "exclusion_reason": self.exclusion_reason,
            "terms": [term.as_dict() for term in self.terms],
            "readings": [reading.as_dict() for reading in self.readings],
            "rationale": self.rationale(),
        }


@dataclass
class DutySelection:
    """What the engine decided for one duty, and everything it passed over."""

    duty: Duty
    chosen: Placement | None
    considered: list[Placement] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "duty": self.duty.as_dict(),
            "chosen": self.chosen.as_dict() if self.chosen else None,
            "considered": [row.as_dict() for row in self.considered],
            "notes": list(self.notes),
        }


def _fit(option: ModelOption, work_type: str) -> Term:
    desired = DESIRED_STRENGTH[work_type]
    distance = abs(option.strength - desired)
    value = max(0.0, 1.0 - distance / STRENGTH_SPAN) ** FIT_CURVE
    return Term(
        name="fit",
        value=value,
        weight=0.0,
        basis="policy",
        detail=(
            f"{work_type} work wants strength {desired}; {option.model} is "
            f"strength {option.strength}"
        ),
    )


def _cost(option: ModelOption) -> Term:
    value = (4 - option.cost) / 3.0
    return Term(
        name="cost",
        value=value,
        weight=0.0,
        basis="policy",
        detail=f"{option.model} sits at cost rank {option.cost} of 4",
    )


def _relative_headroom(
    load: float | None, peer_loads: Sequence[float]
) -> tuple[float, str]:
    """Rank an account against its peers when no percentage exists.

    With no published allowance the only comparable fact is that one account
    has spent more than another inside the same window. The heaviest peer sits
    at the bottom of the range and an idle account at the top. When every peer
    is idle there is nothing to separate them, so they all sit at the top.
    """
    if load is None:
        return UNKNOWN_PRIOR, "unknown"
    highest = max(peer_loads) if peer_loads else 0.0
    if highest <= 0:
        return 1.0, "relative-load"
    return max(0.0, 1.0 - (load / highest)), "relative-load"


def _window_term(
    name: str,
    reading: QuotaReading | None,
    *,
    peer_loads: Sequence[float],
    now: int,
    short: bool,
) -> Term:
    if reading is None:
        return Term(
            name=name,
            value=UNKNOWN_PRIOR,
            weight=0.0,
            basis="unknown",
            detail="no local reader produced a figure for this window",
        )
    if reading.used_fraction is not None:
        value = max(0.0, 1.0 - reading.used_fraction)
        detail = (
            f"{reading.used_fraction * 100:.0f}% used, "
            f"{value * 100:.0f}% left ({reading.source})"
        )
        basis = reading.confidence
    else:
        value, basis = _relative_headroom(reading.load_units, peer_loads)
        units = reading.load_units or 0.0
        detail = (
            f"no published allowance; {units:,.0f} {reading.load_unit or 'units'} "
            f"spent in this window ({reading.source})"
        )
    raw = value
    if short and reading.resets_at_unix:
        remaining = reading.resets_at_unix - now
        if 0 < remaining <= IMMINENT_RESET_SECONDS and value < IMMINENT_RESET_FLOOR:
            value = IMMINENT_RESET_FLOOR
            detail += f"; resets in {remaining // 60}m, so not written off"
    return Term(
        name=name,
        value=value,
        weight=0.0,
        basis=basis,
        detail=detail,
        raw_value=raw,
    )


def _weighted(terms: Sequence[Term], weights: Mapping[str, float]) -> list[Term]:
    total = sum(weights.get(term.name, 0.0) for term in terms)
    if total <= 0:
        raise ScoringError("duty weights cannot all be zero")
    return [
        Term(
            name=term.name,
            value=term.value,
            weight=round(weights.get(term.name, 0.0) / total, 4),
            basis=term.basis,
            detail=term.detail,
            raw_value=term.raw_value,
        )
        for term in terms
    ]


def _model_scoped_exclusion(
    ref: AccountRef, option: ModelOption, snapshot: QuotaSnapshot
) -> str:
    """Refuse a placement whose *model* was refused, leaving the account alone.

    "You've reached your Fable 5 limit" takes one model off the table. Every
    other model on that account still works, and treating the account as spent
    would idle a healthy subscription.
    """
    identifier = qs.normalized_model(option.model_id)
    for reading in snapshot.model_scoped(ref.provider, ref.alias):
        hint = reading.model_hint
        if len(hint) < 3 or not reading.exhausted:
            continue
        if hint in identifier or hint == qs.normalized_model(option.model):
            return f"the provider refused this model's own limit ({reading.source})"
    return ""


def _exclusion(
    ref: AccountRef,
    snapshot: QuotaSnapshot,
    now: int,
) -> str:
    if not ref.enabled:
        return "account is disabled in the kit registry"
    for window in qs.WINDOWS:
        reading = snapshot.best(ref.provider, ref.alias, window)
        if reading is None:
            continue
        if reading.exhausted:
            # The reset time is on the reading; the proposal renders it in
            # human terms rather than repeating a unix stamp here.
            return f"{window} window is exhausted ({reading.source})"
        if (
            reading.used_fraction is not None
            and reading.used_fraction >= EXHAUSTION_FRACTION
        ):
            return (
                f"{window} window is {reading.used_fraction * 100:.0f}% used "
                f"({reading.source})"
            )
    return ""


def _peer_loads(
    refs: Sequence[AccountRef],
    snapshot: QuotaSnapshot,
    provider: str,
    window: str,
) -> list[float]:
    loads: list[float] = []
    for ref in refs:
        if ref.provider != provider:
            continue
        reading = snapshot.best(provider, ref.alias, window)
        if reading is not None and reading.load_units is not None:
            loads.append(reading.load_units)
    return loads


def score_duty(
    duty: Duty,
    refs: Sequence[AccountRef],
    snapshot: QuotaSnapshot,
    *,
    catalog: Sequence[ModelOption] | None = None,
    weights: Mapping[str, float] | None = None,
    now: int | None = None,
) -> DutySelection:
    """Score every placement for one duty; return the pick and the runners-up."""
    options = list(catalog) if catalog is not None else load_catalog()
    term_weights = dict(weights) if weights is not None else load_weights()
    stamp = snapshot.taken_at_unix if now is None else int(now)
    peer_cache: dict[tuple[str, str], list[float]] = {}
    placements: list[Placement] = []
    for ref in refs:
        excluded_reason = _exclusion(ref, snapshot, stamp)
        readings = tuple(snapshot.for_account(ref.provider, ref.alias))
        for option in options:
            if option.provider != ref.provider:
                continue
            reason = excluded_reason or _model_scoped_exclusion(ref, option, snapshot)
            terms: list[Term] = [_fit(option, duty.work_type)]
            for name, window, short in (
                ("weekly", qs.WEEKLY, False),
                ("window", qs.FIVE_HOUR, True),
            ):
                cache_key = (ref.provider, window)
                if cache_key not in peer_cache:
                    peer_cache[cache_key] = _peer_loads(
                        refs, snapshot, ref.provider, window
                    )
                terms.append(
                    _window_term(
                        name,
                        snapshot.best(ref.provider, ref.alias, window),
                        peer_loads=peer_cache[cache_key],
                        now=stamp,
                        short=short,
                    )
                )
            terms.append(_cost(option))
            scored = _weighted(terms, term_weights)
            total = sum(term.contribution for term in scored)
            placements.append(
                Placement(
                    duty_id=duty.duty_id,
                    provider=ref.provider,
                    model=option.model,
                    model_id=option.model_id,
                    account=ref.alias,
                    email=ref.email,
                    score=total,
                    terms=tuple(scored),
                    readings=readings,
                    excluded=bool(reason),
                    exclusion_reason=reason,
                )
            )
    # Deterministic: score first, then a stable name order, so two runs on the
    # same inputs produce the same plan and a diff means something changed.
    placements.sort(key=lambda row: (row.excluded, -row.score, row.key))
    chosen = next((row for row in placements if not row.excluded), None)
    notes: list[str] = []
    if chosen is None:
        notes.append(
            "no eligible placement: every account is disabled or out of window"
        )
    # The note explains the pick, so it reads the pick's terms. Sorting puts
    # every excluded row last, so this is normally placements[0] anyway --
    # naming `chosen` keeps it true in the case where nothing was eligible and
    # placements[0] is a placement the plan is not proposing.
    unknown = {
        term.name
        for term in (chosen.terms if chosen else ())
        if term.basis == "unknown"
    }
    if unknown:
        notes.append(
            "scored on a neutral prior for: "
            + ", ".join(sorted(unknown))
            + " — no local reader published a figure"
        )
    return DutySelection(duty=duty, chosen=chosen, considered=placements, notes=notes)


def score_duties(
    duties: Sequence[Duty],
    refs: Sequence[AccountRef],
    snapshot: QuotaSnapshot,
    *,
    catalog: Sequence[ModelOption] | None = None,
    weights: Mapping[str, float] | None = None,
    now: int | None = None,
) -> list[DutySelection]:
    options = list(catalog) if catalog is not None else load_catalog()
    term_weights = dict(weights) if weights is not None else load_weights()
    return [
        score_duty(
            duty,
            refs,
            snapshot,
            catalog=options,
            weights=term_weights,
            now=now,
        )
        for duty in duties
    ]
