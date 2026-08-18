"""Shared rendering for the README's designed figures.

Every figure here is laid out by a real browser rather than by hand-placed
coordinates. The previous SVG figures positioned each line of text at a fixed
x/y in a font (Inter) that almost no reader has installed, so the layout only
held while the fallback font happened to be narrow enough; three lines in the
safety figure crossed their own card borders. Letting the browser size boxes to
their content removes that entire class of bug, and `assert_nothing_overflows`
fails the render if any element ever exceeds its box again.

The font is embedded from disk, so a render does not depend on a webfont host
being reachable and produces the same bytes every time.
"""

from __future__ import annotations

import base64
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
ASSETS = REPO / "docs" / "assets" / "readme"

# Chromium ships out of band from the Python package, so the revision the
# package expects and the revision on disk drift. Find whatever is here.
CHROMIUM_CANDIDATES = (
    "chromium/chrome-linux/chrome",
    "chromium_headless_shell/chrome-linux/headless_shell",
)

INTER = Path.home() / ".local/share/fonts/InterVariable.ttf"
MONO = Path("/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono.ttf")
MONO_FALLBACKS = (
    MONO,
    Path.home() / ".local/share/fonts/DejaVuSansMono.ttf",
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
)

# The picker's own palette, so a figure never disagrees with the screenshots.
INK = {
    "bg": "#0d1117",
    "panel": "#161b22",
    "raised": "#21262d",
    "line": "#30363d",
    "text": "#f0f6fc",
    "dim": "#8b949e",
    "green": "#9ece6a",
    "cyan": "#2ac3de",
    "amber": "#e0af68",
    "purple": "#bb9af7",
    "red": "#f7768e",
}


def chromium_path() -> Path | None:
    """The chromium binary Playwright downloaded, whatever revision it is."""

    root = Path.home() / ".cache" / "ms-playwright"
    if not root.is_dir():
        return None
    for entry in sorted(root.iterdir(), reverse=True):
        for suffix in CHROMIUM_CANDIDATES:
            stem = suffix.split("/", 1)[0]
            if not entry.name.startswith(stem):
                continue
            candidate = entry / suffix.split("/", 1)[1]
            if candidate.is_file():
                return candidate
    return None


def _mono_font() -> Path | None:
    for candidate in MONO_FALLBACKS:
        if candidate.is_file():
            return candidate
    return None


def _face(path: Path, family: str, extra: str = "") -> str:
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return (
        f"@font-face{{font-family:'{family}';{extra}"
        f"src:url(data:font/ttf;base64,{payload}) format('truetype');}}"
    )


def font_faces() -> str:
    """Embed the fonts so a figure never depends on a network font host.

    A missing font is not fatal, the browser falls back and the figure still
    lays out, but it does change every pixel, which the approved-digest gate
    then rejects with a message about digests rather than about fonts. Say
    which font is missing while that is still the useful thing to know.
    """

    faces = []
    missing = []
    if INTER.is_file():
        faces.append(_face(INTER, "Inter", "font-weight:100 900;"))
    else:
        missing.append(str(INTER))
    mono = _mono_font()
    if mono is not None:
        faces.append(_face(mono, "KitMono"))
    else:
        missing.append(" or ".join(str(path) for path in MONO_FALLBACKS))
    if missing:
        print(
            "warning: rendering without an embedded font, so these bytes will "
            "not match the approved digests. Missing: " + "; ".join(missing),
            file=sys.stderr,
        )
    return "".join(faces)


BASE_CSS = """
*{{box-sizing:border-box;margin:0;padding:0}}
body{{
  font-family:Inter,system-ui,sans-serif;
  background:{bg};color:{text};
  -webkit-font-smoothing:antialiased;
  font-feature-settings:'cv05' 1,'ss03' 1;
}}
.mono{{font-family:KitMono,ui-monospace,monospace}}
"""


def page(body: str, css: str, *, width: int, height: int) -> str:
    """One self-contained document, sized exactly to the figure."""

    shell = BASE_CSS.format(**INK)
    return (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        + font_faces()
        + shell
        + f"body{{width:{width}px;height:{height}px;overflow:hidden}}"
        + css
        + "</style></head><body>"
        + body
        + "</body></html>"
    )


# Every element must fit inside itself. A browser will happily let text spill
# out of a fixed box rather than complain, which is exactly how the old figure
# shipped broken, so the render asks the page directly before saving.
OVERFLOW_PROBE = """
() => {
  const bad = [];
  for (const el of document.querySelectorAll('*')) {
    if (el.tagName === 'BODY' || el.tagName === 'HTML') continue;
    const style = getComputedStyle(el);
    if (style.overflow === 'visible' && !el.dataset.fixed) continue;
    if (el.scrollWidth > el.clientWidth + 1 ||
        el.scrollHeight > el.clientHeight + 1) {
      bad.push(`${el.tagName}.${el.className||'-'} ` +
               `content ${el.scrollWidth}x${el.scrollHeight} ` +
               `box ${el.clientWidth}x${el.clientHeight}`);
    }
  }
  return bad;
}
"""

# Text that leaves the page entirely is the failure the old figures actually
# shipped, and a clipped ancestor hides it from the probe above. Measure every
# text-bearing element against the viewport as well.
ESCAPE_PROBE = """
(size) => {
  const bad = [];
  for (const el of document.querySelectorAll('*')) {
    if (!el.textContent || !el.textContent.trim()) continue;
    if (el.children.length) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    if (r.left < -0.5 || r.top < -0.5 ||
        r.right > size.w + 0.5 || r.bottom > size.h + 0.5) {
      bad.push(`"${el.textContent.trim().slice(0, 46)}" at ` +
               `${r.left.toFixed(0)},${r.top.toFixed(0)} ` +
               `${r.right.toFixed(0)}x${r.bottom.toFixed(0)}`);
    }
  }
  return bad;
}
"""


class RenderError(RuntimeError):
    pass


# How tall the content actually is, so a figure is not padded out with dead
# space and does not need a hand-maintained height constant that drifts every
# time a sentence changes.
CONTENT_HEIGHT = """
() => {
  let bottom = 0;
  for (const el of document.body.children) {
    bottom = Math.max(bottom, el.getBoundingClientRect().bottom);
  }
  const pad = parseFloat(getComputedStyle(document.body).paddingBottom) || 0;
  return Math.ceil(bottom + pad);
}
"""


def measure_height(html: str, *, width: int, probe_height: int = 4000) -> int:
    """Lay the figure out once and report the height its content needs."""

    from playwright.sync_api import sync_playwright

    binary = chromium_path()
    if binary is None:
        raise RenderError("no chromium available")
    with sync_playwright() as play:
        browser = play.chromium.launch(executable_path=str(binary))
        try:
            page_ = browser.new_page(viewport={"width": width, "height": probe_height})
            page_.set_content(
                html.replace(f"height:{probe_height}px", "height:auto"),
                wait_until="load",
            )
            page_.wait_for_timeout(200)
            return int(page_.evaluate(CONTENT_HEIGHT))
        finally:
            browser.close()


def render(
    html: str,
    output: Path,
    *,
    width: int,
    height: int,
    scale: int = 2,
) -> Path:
    """Screenshot one figure, refusing to save a layout that does not fit."""

    from playwright.sync_api import sync_playwright

    binary = chromium_path()
    if binary is None:
        raise RenderError(
            "no chromium available; install one with `python3 -m playwright "
            "install chromium`"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as play:
        browser = play.chromium.launch(executable_path=str(binary))
        try:
            page_ = browser.new_page(
                viewport={"width": width, "height": height},
                device_scale_factor=scale,
            )
            page_.set_content(html, wait_until="load")
            page_.wait_for_timeout(250)
            overflow = page_.evaluate(OVERFLOW_PROBE)
            escaped = page_.evaluate(ESCAPE_PROBE, {"w": width, "h": height})
            problems = list(overflow) + list(escaped)
            if problems:
                raise RenderError(
                    f"{output.name}: content does not fit its layout:\n  "
                    + "\n  ".join(problems)
                )
            page_.screenshot(path=str(output))
        finally:
            browser.close()
    return output


# ---------------------------------------------------------------------------
# The terminal window every picker picture is drawn inside.
#
# The contents come from the real picker; this only supplies the chrome. It
# lives here rather than in one of the render tools so the still and the
# animation cannot drift apart, and so neither has to import an extensionless
# script by path.

COLUMNS = 104
TYPE_SIZE = 26
CELL = TYPE_SIZE * 0.60227
LINE_HEIGHT = 1.42
PAD_X = 34
PAD_Y = 26
TITLE_BAR = 54

WINDOW_CSS = """
body{{background:{outer};display:flex;align-items:center;justify-content:center}}
.win{{
  width:{win}px;border-radius:14px;overflow:hidden;
  background:{bg};border:1px solid {line};
  box-shadow:0 18px 50px rgba(0,0,0,.55);
}}
.bar{{
  height:{bar}px;background:{raised};border-bottom:1px solid {line};
  display:flex;align-items:center;padding:0 18px;position:relative;
}}
.lights{{display:flex;gap:9px}}
.lights i{{width:13px;height:13px;border-radius:50%;display:block}}
.title{{
  position:absolute;left:0;right:0;text-align:center;
  font-family:KitMono,monospace;font-size:17px;color:{dim};
  letter-spacing:.08em;pointer-events:none;
}}
.screen{{
  padding:{pady}px {padx}px;
  font-family:KitMono,monospace;font-size:{size}px;line-height:{lh};
  color:#c9d1d9;white-space:pre;
}}
.caret{{
  display:inline-block;width:{cell}px;height:{caret}px;
  background:{text};vertical-align:-3px;margin-left:2px;
}}
"""


def window_css() -> str:
    return WINDOW_CSS.format(
        outer=INK["bg"],
        bg=INK["bg"],
        raised=INK["raised"],
        line=INK["line"],
        dim=INK["dim"],
        text=INK["text"],
        win=round(COLUMNS * CELL) + PAD_X * 2,
        bar=TITLE_BAR,
        padx=PAD_X,
        pady=PAD_Y,
        size=TYPE_SIZE,
        lh=LINE_HEIGHT,
        cell=round(CELL),
        caret=round(TYPE_SIZE * 1.05),
    )


def window_html(body: str, rows: int, *, caret: bool = True) -> tuple[str, int, int]:
    """A captured screen inside a terminal window, sized to its own text."""

    win = round(COLUMNS * CELL) + PAD_X * 2
    height = TITLE_BAR + PAD_Y * 2 + round(rows * TYPE_SIZE * LINE_HEIGHT)
    tail = '<span class="caret"></span>' if caret else ""
    markup = (
        '<div class="win">'
        '<div class="bar">'
        '<div class="lights">'
        '<i style="background:#ff5f57"></i>'
        '<i style="background:#febc2e"></i>'
        '<i style="background:#28c840"></i>'
        "</div>"
        '<div class="title">session kit</div>'
        "</div>"
        f'<div class="screen">{body}{tail}</div>'
        "</div>"
    )
    # A little room around the window so the drop shadow is not clipped.
    return markup, win + 40, height + 40
