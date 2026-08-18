"""What every choice on the screen actually runs.

The screen never touches a session itself. It names an `sp` command, the same
proof-bound commands the picker has always used, and hands it to the runner.
Planning is separated from running so the exact command line each choice
produces is testable without starting anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from . import voice
from .model import ClosedSession, Session

# The placeholder the runner replaces with a freshly written proof file. A
# proof names the exact session, so an action can never land on the row that
# took its place during a repaint.
PROOF = "{proof}"

# Claude Code's palette then the Codex-only one, in palette order.
CLAUDE_COLORS = ("red", "blue", "green", "yellow", "purple", "orange", "pink", "cyan")
CODEX_COLORS = ("lime", "magenta", "silver", "sand", "sky", "sea")


@dataclass(frozen=True)
class Plan:
    """One command, and the sentence the screen prints once it has run."""

    argv: tuple[str, ...]
    success: str
    failure: str
    needs_terminal: bool = False
    proof_for: str | None = None
    confirm_id: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    # A verb the release may not carry yet. Its own refusal is the honest
    # answer, so the screen shows that line rather than inventing one.
    quote_stderr: bool = False

    @property
    def wants_proof(self) -> bool:
        return PROOF in self.argv


def _selector(session: Session) -> str:
    """Numbers are what a person sees; the proof is what names the session.

    Where a verb takes no proof, the number is the only selector the screen is
    allowed to pass on: an ID would have to be printed to be typed, and no
    screen prints one.
    """

    return str(session.number) if session.number is not None else (session.shpool_id or "")


def open_session(session: Session) -> Plan:
    verb = "picker-takeover" if session.is_attached else "picker-open"
    return Plan(
        argv=("sp", verb, PROOF),
        success=voice.opened(session.title),
        failure=voice.action_failed(voice.ACTION_OPEN),
        needs_terminal=True,
        proof_for=session.key,
        confirm_id=session.shpool_id,
    )


def history(session: Session) -> Plan:
    return Plan(
        argv=("sp", "picker-history", PROOF),
        success="",
        failure=voice.action_failed(voice.ACTION_HISTORY),
        needs_terminal=True,
        proof_for=session.key,
        confirm_id=session.shpool_id,
    )


def close(session: Session) -> Plan:
    return Plan(
        argv=("sp", "picker-close", PROOF),
        success=voice.closed_one(session.title),
        failure=voice.action_failed(voice.ACTION_CLOSE),
        proof_for=session.key,
        confirm_id=session.shpool_id,
    )


def rename(session: Session, title: str) -> Plan:
    return Plan(
        argv=("sp", "picker-name", PROOF, title),
        success=voice.renamed(title),
        failure=voice.action_failed(voice.ACTION_RENAME),
        proof_for=session.key,
        confirm_id=session.shpool_id,
    )


def change_account(session: Session, alias: str) -> Plan:
    return Plan(
        argv=("sp", "picker-account-switch", PROOF, alias),
        success=voice.account_changed(session.title, alias),
        failure=voice.action_failed(voice.ACTION_ACCOUNT),
        needs_terminal=True,
        proof_for=session.key,
        confirm_id=session.shpool_id,
    )


def change_model(session: Session, model: str) -> Plan:
    """`sp change-model` is the verb this screen calls.

    A release without it refuses in its own words, and those are the words the
    screen shows, inventing a friendlier line would hide which release is
    installed.
    """

    return Plan(
        argv=("sp", "change-model", _selector(session), model),
        success=voice.model_changed(session.title, model),
        failure=voice.action_failed(voice.ACTION_MODEL),
        quote_stderr=True,
    )


def color(session: Session, name: str) -> Plan:
    return Plan(
        argv=("sp", "color", _selector(session), name),
        success=voice.color_changed(session.title, name),
        failure=voice.action_failed(voice.ACTION_COLOR),
    )


def restore(item: ClosedSession) -> Plan:
    argv = ["sp", "restore-exact", item.provider, item.uuid, item.cwd or "."]
    if item.account_alias:
        argv.append(item.account_alias)
    return Plan(
        argv=tuple(argv),
        success=voice.restored(item.title),
        failure=voice.action_failed(voice.ACTION_RESTORE),
        needs_terminal=True,
    )


def new_session(provider: str, project: str | None = None) -> Plan:
    argv = ["sp", "new", provider]
    if project:
        argv.append(project)
    return Plan(
        argv=tuple(argv),
        success="",
        failure=voice.action_failed(voice.NEW_SESSION),
        needs_terminal=True,
    )


def colors_for(provider: str) -> tuple[str, ...]:
    return CODEX_COLORS if provider.casefold() == "codex" else CLAUDE_COLORS
