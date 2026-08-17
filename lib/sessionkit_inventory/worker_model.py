"""Provider-specific worker model validation, and whether it is really served.

Extracted verbatim from the retired ``sessionkit_supervisor.intake`` module
(2026-08-12 one-door rebuild): the ``validate-worker-model`` verb sits on the
live launch path — ``bashrc/shpool.bashrc`` and ``sp new`` both call it before
starting a provider — so the gate survives the supervisor's deletion.

That gate only ever checked the *shape* of an identifier. A model can be
perfectly well-formed, accepted by the provider's command line, and quietly
served by something smaller — which is how a session runs for an hour on a
model nobody chose. So there is a second question here, asked before a session
starts and never answered by guessing:

    is this model actually served on this machine?

Two local sources answer it, in this order:

* **What was served last time.** When something reads the model a session is
  really on, it records the pair here with :func:`record_served`. A recorded
  answer that differs from the request is the silent downgrade, by name.
* **The machine's own model list** (``models.tsv``), which is the operator
  saying what this host offers.

With neither, the answer is ``unknown`` — said as unknown, never as verified.
Nothing in this module ever substitutes a model: it refuses, names what would
serve the request instead, and leaves the choice to the person.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Iterator, Mapping, Sequence

MODEL_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")

# Verdicts. The first three start a session; the last two refuse one.
SERVED = "served"
OFFERED = "offered"
UNKNOWN = "unknown"
NOT_OFFERED = "not-offered"
DOWNGRADED = "downgraded"
REFUSALS = (NOT_OFFERED, DOWNGRADED)

SERVED_FILE = "served-models.tsv"
MAX_SERVED_BYTES = 256 * 1024
MAX_SERVED_ROWS = 512


class IntakeError(ValueError):
    """A requested worker model is invalid."""


def _text(value: object, *, limit: int, label: str, required: bool = True) -> str:
    """One bounded line: control characters stripped, whitespace flattened."""
    if value is None:
        if required:
            raise IntakeError(f"{label} is required")
        return ""
    if not isinstance(value, str):
        raise IntakeError(f"{label} must be a string")
    flattened = " ".join(
        "".join(character for character in value if character >= " ").split()
    )
    if required and not flattened:
        raise IntakeError(f"{label} is required")
    return flattened[:limit]


def _model(provider: str, value: object) -> str:
    model = _text(value, limit=128, label="requested model")
    if not MODEL_RE.fullmatch(model):
        raise IntakeError("requested model is not a supported identifier")
    if provider == "claude" and not model.startswith("claude-"):
        raise IntakeError("a Claude worker needs a Claude model identifier")
    if provider == "codex" and not model.startswith(("gpt-", "o3", "o4", "codex-")):
        raise IntakeError("a Codex worker needs a Codex model identifier")
    return model


def validate_requested_model(provider: object, value: object) -> str:
    """Single provider-specific model gate shared by launch and ``sp new``."""
    if provider not in ("claude", "codex"):
        raise IntakeError("worker provider must be claude or codex")
    return _model(str(provider), value)


# ---- what was actually served -------------------------------------------


def served_path(state_dir: Path | str) -> Path:
    return Path(state_dir) / "models" / SERVED_FILE


def read_served(state_dir: Path | str) -> dict[tuple[str, str], dict[str, Any]]:
    """Every recorded (provider, requested) -> what really served it."""
    path = served_path(state_dir)
    try:
        if path.is_symlink() or not path.is_file():
            return {}
        if path.stat().st_size > MAX_SERVED_BYTES:
            return {}
        text = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 3:
            continue
        provider, requested, served = (value.strip() for value in fields[:3])
        if provider not in ("claude", "codex") or not requested or not served:
            continue
        try:
            observed = int(fields[3]) if len(fields) > 3 else 0
        except ValueError:
            observed = 0
        rows[(provider, requested)] = {"served": served, "at_unix_ms": observed}
    return rows


def record_served(
    state_dir: Path | str,
    provider: str,
    requested: str,
    served: str,
    *,
    now_unix_ms: int | None = None,
) -> dict[str, Any]:
    """Record the model a request was really answered with.

    This is the one call anything reading a live session's actual model needs
    to make. The newest observation for a pair replaces the older one — a model
    that has come back is as important as one that went away.
    """
    provider = str(provider)
    if provider not in ("claude", "codex"):
        raise IntakeError("worker provider must be claude or codex")
    requested_model = _model(provider, requested)
    served_model = _model(provider, served)
    stamp = int(time.time() * 1000) if now_unix_ms is None else int(now_unix_ms)
    rows = read_served(state_dir)
    rows[(provider, requested_model)] = {
        "served": served_model,
        "at_unix_ms": stamp,
    }
    ordered = sorted(
        rows.items(), key=lambda item: int(item[1]["at_unix_ms"]), reverse=True
    )[:MAX_SERVED_ROWS]
    payload = "".join(
        f"{key[0]}\t{key[1]}\t{value['served']}\t{value['at_unix_ms']}\n"
        for key, value in sorted(ordered)
    ).encode("utf-8")
    path = served_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return {
        "provider": provider,
        "requested": requested_model,
        "served": served_model,
        "at_unix_ms": stamp,
        "downgraded": served_model != requested_model,
    }


def availability(
    provider: str,
    model: str,
    *,
    state_dir: Path | str,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Whether this machine really serves the model that was asked for.

    Never substitutes and never guesses: it answers ``served``, ``offered``,
    ``unknown``, or one of the two refusals, and when it refuses it names what
    would serve the request instead.
    """
    environ = os.environ if environ is None else environ
    wanted = validate_requested_model(provider, model)
    offered = configured_models(str(provider), environ)
    observed = read_served(state_dir).get((str(provider), wanted))
    if observed is not None:
        served_model = str(observed["served"])
        if served_model != wanted:
            return {
                "provider": provider,
                "model": wanted,
                "verdict": DOWNGRADED,
                "serves": served_model,
                "offered": list(offered),
                "observed_at_unix_ms": int(observed["at_unix_ms"]),
                "reason": (
                    f"the last session that asked for {wanted} actually ran on "
                    f"{served_model}"
                ),
            }
        return {
            "provider": provider,
            "model": wanted,
            "verdict": SERVED,
            "serves": wanted,
            "offered": list(offered),
            "observed_at_unix_ms": int(observed["at_unix_ms"]),
            "reason": f"{wanted} served a session on this machine before",
        }
    if offered:
        if wanted in offered:
            return {
                "provider": provider,
                "model": wanted,
                "verdict": OFFERED,
                "serves": wanted,
                "offered": list(offered),
                "reason": f"{wanted} is on this machine's model list",
            }
        return {
            "provider": provider,
            "model": wanted,
            "verdict": NOT_OFFERED,
            "serves": "",
            "offered": list(offered),
            "reason": f"{wanted} is not on this machine's model list",
        }
    return {
        "provider": provider,
        "model": wanted,
        "verdict": UNKNOWN,
        "serves": "",
        "offered": [],
        "reason": (
            "this machine has no model list and has not seen this model serve "
            "a session, so nothing here can confirm it"
        ),
    }


def render_availability(verdict: Mapping[str, Any], *, flag: str) -> str:
    """What a person reads when a launch is refused, in their words."""
    model = str(verdict.get("model") or "")
    serves = str(verdict.get("serves") or "")
    offered: Sequence[str] = list(verdict.get("offered") or [])
    lines = [f"You asked for {model}."]
    if verdict.get("verdict") == DOWNGRADED:
        lines.append(
            f"The last session that asked for it actually ran on {serves} instead."
        )
        lines.append(
            f"Start it on {serves} with --model {serves}, or repeat the command "
            f"with {flag} to ask for {model} anyway."
        )
    else:
        lines.append("This machine's model list does not offer it.")
        if offered:
            lines.append("It offers: " + ", ".join(offered) + ".")
        lines.append(
            f"Name one of those, add {model} to the list, or repeat the command "
            f"with {flag} to ask for it anyway."
        )
    return "\n".join(lines) + "\n"


def configured_models(provider: str, environ: Mapping[str, str]) -> tuple[str, ...]:
    """Configured model IDs accepted by the model-change verb.

    Configuration chooses the catalog, but the launch-path validator chooses
    which entries are valid for a provider. Both pickers and the destructive
    verb therefore share one acceptance rule and never guess a catalog.
    """
    inline = environ.get("SESSION_KIT_TUI_MODELS", "")
    candidates: Iterator[str]
    if inline.strip():
        candidates = (part.strip() for part in inline.split(","))
    else:
        path = environ.get("SESSION_KIT_MODELS_FILE") or str(
            Path(environ.get("HOME", "/tmp")) / ".config/session-kit/models.tsv"
        )
        found: list[str] = []
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) < 2 or parts[0].casefold() not in {
                        provider.casefold(),
                        "*",
                    }:
                        continue
                    found.append(parts[1].strip())
        except OSError:
            return ()
        candidates = iter(found)
    valid: list[str] = []
    for candidate in candidates:
        candidate = " ".join(
            "".join(character for character in candidate if character >= " ").split()
        )[:128]
        if not candidate:
            continue
        try:
            candidate = validate_requested_model(provider, candidate)
        except IntakeError:
            continue
        if candidate not in valid:
            valid.append(candidate)
    return tuple(valid)
