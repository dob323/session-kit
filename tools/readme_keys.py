"""The README's key table and the picker's command bar have to agree.

The picker in the README's screenshot is rendered from live code, so a key
merged privately and not yet released appears in the picture the moment someone
regenerates it, advertising a key on the project's front page that the shipped
release does not have. This refuses to render instead.

The checks used to live inside `tools/render-readme-anatomy`, which drew an
annotated four-panel figure the README no longer carries. The figure went; the
guard is the part worth keeping, so it moved here and now runs against the
screenshot that actually ships.
"""

from __future__ import annotations

from pathlib import Path
import re

REPO = Path(__file__).resolve().parents[1]


def documented_keys(readme: Path | None = None) -> set[str]:
    """The single-character keys the README's key table currently promises."""

    table = (readme or REPO / "README.md").read_text(encoding="utf-8")
    keys = set()
    for line in re.findall(r"^\|.*\|\s*$", table, re.MULTILINE):
        # A row may name more than one key, as "`b` or `q`" does.
        for span in re.findall(r"`([^`]+)`", line):
            head = span.split()[0]
            if len(head) == 1 and (head.isalpha() or head == "?"):
                keys.add(head)
    if not keys:
        raise RuntimeError("the README key table named no keys")
    return keys


def offered_keys(frame: str) -> set[str]:
    """The single-character keys the rendered command bar actually offers."""

    footer = next(
        (line for line in frame.splitlines() if "kill" in line and "leave" in line), ""
    )
    if not footer:
        raise RuntimeError("the capture had no command bar to check")
    # The frame carries its colour escapes; they sit flush against the keys.
    plain = re.sub(r"\x1b\[[0-9;]*m", "", footer)
    return {token for token in re.findall(r"(?<!\S)([a-z?])(?!\S)", plain)}


def refuse_undocumented_keys(frame: str, readme: Path | None = None) -> None:
    """A key the picker offers but the README does not document must not ship."""

    extra = sorted(offered_keys(frame) - documented_keys(readme))
    if extra:
        raise SystemExit(
            "the picker offers keys the README does not document: "
            + ", ".join(extra)
            + "\nEither document them in the README key table, or render this "
            "figure from a tree where the feature is not present. Shipping it "
            "would advertise a key the released version does not have."
        )
