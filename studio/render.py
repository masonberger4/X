"""Draw a studio card: one self-contained HTML file -> a 2x PNG, plus a layout report.

The session designs each card as HTML (studio/brief/cards.md); the app, not the session,
turns it into a picture, so the session needs no shell and the same renderer runs on the
operator's PC and in tests. A Chromium-family browser does the drawing in headless mode:
Microsoft Edge on Windows (installed with Windows), Chrome or Chromium elsewhere, or the
path in studio/config.yaml `render.browser` (STUDIO_BROWSER env wins).

Two browser runs per card, both with the network blocked (`--host-resolver-rules`), so a
card can never fetch anything and rendering is the same offline:
1. `--dump-dom` on a copy of the page with a checker script added. The script waits for
   the fonts, then reports text that overflows its box, runs off the canvas or overlaps
   other text. Those reports go back to the session to fix.
2. `--screenshot` of the original page at the card's size and 2x scale.

Both copies open with a content policy (`CONTENT_POLICY`) that lets a card use its own
inline styles and SVG, data: images and the house fonts, and nothing else: no frame,
object, script, stylesheet or image from the web or the disk. The browser can read any
file the app can, and the session opens the pictures, so without it a card could draw
another file (a .env, a key) into its own picture. A card that tries to navigate with a
meta refresh is refused before the browser starts.

The house fonts (Inter, IBM Plex Mono; SIL Open Font License) ship in studio/fonts and are
injected as @font-face rules, so a card names them by family and needs nothing external.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_SIZE = (1080, 1350)  # 4:5, CSS pixels
WIDE_SIZE = (1600, 900)  # 16:9, for a timeline that cannot work in portrait
SCALE = 2
EDGE_MARGIN = 20  # nothing may sit closer than this to the canvas edge
FONT_DIR = Path(__file__).resolve().parent / "fonts"
FONTS = (
    (
        "Inter",
        "inter-latin-wght-normal.woff2",
        "100 900",
        "U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD",
    ),
    (
        "Inter",
        "inter-latin-ext-wght-normal.woff2",
        "100 900",
        "U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+0304,U+0308,U+0329,U+1D00-1DBF,U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF",
    ),
    (
        "Inter",
        "inter-greek-wght-normal.woff2",
        "100 900",
        "U+0370-0377,U+037A-037F,U+0384-038A,U+038C,U+038E-03A1,U+03A3-03FF",
    ),
    ("IBM Plex Mono", "ibm-plex-mono-latin-400-normal.woff2", "400", ""),
    ("IBM Plex Mono", "ibm-plex-mono-latin-600-normal.woff2", "600", ""),
)
SIZE_META = re.compile(
    r"""<meta\s+name=["']card-size["']\s+content=["'](\d{3,4})\s*x\s*(\d{3,4})["']""", re.I
)
REPORT_ID = "__studio_layout_report"
# What a card may load. The checker script runs by its nonce; a card's own scripts never do.
CONTENT_POLICY = (
    "default-src 'none'; style-src 'unsafe-inline'; font-src file: data:; img-src data:; "
    "script-src 'nonce-{nonce}'"
)
# Whatever may come before the doctype without putting the page in quirks mode: the
# injected head goes after it.
_PROLOGUE = re.compile(r"\A\ufeff?(?:\s+|<!--.*?-->|<\?[^>]*>)*(?:<!doctype[^>]*>)?", re.I | re.S)
# A meta refresh navigates the page (to another file, say) without any script.
_REFRESH = re.compile(r"""http-equiv\s*=\s*["']?\s*refresh""", re.I)
_EXTERNAL = (
    re.compile(r"""(?:src|href)\s*=\s*["']?\s*(?:(?:https?|file):)?//""", re.I),
    re.compile(r"""url\(\s*['"]?\s*(?:(?:https?|file):)?//""", re.I),
)

# Candidate browsers in the order they are tried. Windows ships Edge, so the desktop
# build needs nothing extra; Linux and macOS use whatever Chromium-family browser exists.
_WINDOWS_CANDIDATES = (
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
)
_MAC_CANDIDATES = (
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)
_PATH_NAMES = (
    "msedge",
    "microsoft-edge",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "chrome",
)


class RenderError(RuntimeError):
    """The card could not be drawn at all (no browser, the browser failed, no PNG)."""


@dataclass
class RenderResult:
    png: Path
    size: tuple[int, int]
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def find_browser(configured: str | None = None) -> str:
    """Path of a Chromium-family browser. STUDIO_BROWSER env, then the configured path,
    then the usual install locations, then PATH. Raises RenderError when none exists."""
    for explicit in (os.environ.get("STUDIO_BROWSER", "").strip(), (configured or "").strip()):
        if explicit:
            found = shutil.which(explicit) or (explicit if Path(explicit).is_file() else None)
            if found:
                return found
            raise RenderError(f"browser {explicit!r} not found")
    candidates: list[str] = []
    if sys.platform == "win32":
        candidates += [os.path.expandvars(c) for c in _WINDOWS_CANDIDATES]
    elif sys.platform == "darwin":
        candidates += list(_MAC_CANDIDATES)
    for c in candidates:
        if Path(c).is_file():
            return c
    for name in _PATH_NAMES:
        found = shutil.which(name)
        if found:
            return found
    pw = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if pw:
        for c in sorted(Path(pw).glob("chromium-*/chrome-linux/chrome"), reverse=True):
            if c.is_file():
                return str(c)
    raise RenderError(
        "no Chromium-family browser found (Edge, Chrome or Chromium); install one or set "
        "render.browser in studio/config.yaml"
    )


def card_size(page: str) -> tuple[int, int]:
    """The card's CSS size: `<meta name="card-size" content="1600x900">` for a wide card,
    else 4:5. Only the two house sizes are accepted."""
    m = SIZE_META.search(page)
    if not m:
        return DEFAULT_SIZE
    size = (int(m.group(1)), int(m.group(2)))
    return size if size in (DEFAULT_SIZE, WIDE_SIZE) else DEFAULT_SIZE


def font_css(font_dir: Path = FONT_DIR) -> str:
    rules = []
    for family, filename, weight, urange in FONTS:
        path = font_dir / filename
        if not path.is_file():
            continue
        rule = (
            f"@font-face{{font-family:'{family}';font-style:normal;font-weight:{weight};"
            f"font-display:block;src:url('{path.as_uri()}') format('woff2');"
        )
        rules.append(rule + (f"unicode-range:{urange};}}" if urange else "}"))
    return "\n".join(rules)


def _checker_script(size: tuple[int, int], nonce: str) -> str:
    """JS that reports overflowing, off-canvas and overlapping text into a <pre>."""
    w, h = size
    return f"""
<script nonce="{nonce}">
(async () => {{
  try {{ await document.fonts.ready; }} catch (e) {{}}
  const W = {w}, H = {h}, M = {EDGE_MARGIN};
  const out = [];
  const label = (el) => (el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 60);
  const leaves = [];
  const clippers = [];  // boxes that hide part of what they hold; their text is checked below
  for (const el of document.body.querySelectorAll('*')) {{
    if (['SCRIPT','STYLE'].includes(el.tagName) || el.id === '{REPORT_ID}') continue;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    const hasText = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());
    if (hasText) leaves.push(el);
    const clips = ['hidden','clip','scroll','auto'].includes(cs.overflowX) ||
                  ['hidden','clip','scroll','auto'].includes(cs.overflowY);
    const hides = el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1;
    // An <svg> cuts its drawing at its own box without ever scrolling, so it always counts.
    const svgBox = el instanceof SVGSVGElement && !el.ownerSVGElement;
    if (clips && hides && hasText) out.push('text is cut off in its box: "' + label(el) + '"');
    else if (clips && (hides || svgBox)) clippers.push(el);
    if (cs.textOverflow === 'ellipsis' && el.scrollWidth > el.clientWidth + 1) {{
      out.push('text is truncated with an ellipsis: "' + label(el) + '"');
    }}
  }}
  const rects = [];
  for (const el of leaves) {{
    const range = document.createRange();
    // A text rect spans the font's whole ascent and descent; under a tight line-height
    // (a hero headline) that reaches well past the line the glyphs sit in, so it is
    // trimmed to the line box.
    const lh = parseFloat(getComputedStyle(el).lineHeight);
    for (const n of el.childNodes) {{
      if (n.nodeType !== 3 || !n.textContent.trim()) continue;
      range.selectNodeContents(n);
      for (const box of range.getClientRects()) {{
        if (box.width < 1 || box.height < 1) continue;
        const trim = lh > 0 && lh < box.height ? (box.height - lh) / 2 : 0;
        const r = {{left: box.left, right: box.right, top: box.top + trim, bottom: box.bottom - trim}};
        rects.push({{el, r}});
        if (r.left < M - 0.5 || r.top < M - 0.5 || r.right > W - M + 0.5 || r.bottom > H - M + 0.5) {{
          out.push('text runs off the card or within ' + M + 'px of its edge: "' + label(el) + '"');
        }}
      }}
    }}
  }}
  for (const box of clippers) {{
    const b = box.getBoundingClientRect();
    for (const {{el, r}} of rects) {{
      // Above the capitals a line is empty, so up to a fifth of it may sit past the box.
      const v = Math.max(2, (r.bottom - r.top) / 5);
      if (box.contains(el) && (r.left < b.left - 2 || r.right > b.right + 2 ||
                               r.top < b.top - v || r.bottom > b.bottom + v)) {{
        out.push('text is cut off in its box: "' + label(el) + '"');
      }}
    }}
  }}
  for (let i = 0; i < rects.length; i++) {{
    for (let j = i + 1; j < rects.length; j++) {{
      const a = rects[i], b = rects[j];
      if (a.el === b.el || a.el.contains(b.el) || b.el.contains(a.el)) continue;
      const ox = Math.min(a.r.right, b.r.right) - Math.max(a.r.left, b.r.left);
      const oy = Math.min(a.r.bottom, b.r.bottom) - Math.max(a.r.top, b.r.top);
      if (ox > 2 && oy > 2) {{
        out.push('text overlaps other text: "' + label(a.el) + '" and "' + label(b.el) + '"');
      }}
    }}
  }}
  if (document.documentElement.scrollWidth > W + 1 || document.documentElement.scrollHeight > H + 1) {{
    out.push('the page is larger than the ' + W + 'x' + H + ' card (' +
             document.documentElement.scrollWidth + 'x' + document.documentElement.scrollHeight + ')');
  }}
  const pre = document.createElement('pre');
  pre.id = '{REPORT_ID}';
  pre.textContent = JSON.stringify([...new Set(out)]);
  document.body.appendChild(pre);
}})();
</script>
"""


def _with_head(page: str, css: str, nonce: str) -> str:
    """The page with the charset, the content policy and the house fonts first in its head:
    straight after the doctype, ahead of everything the card wrote, since a policy only
    covers what comes after it."""
    policy = CONTENT_POLICY.format(nonce=nonce)
    head = (
        '<meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{policy}">'
        f"<style>{css}</style>"
    )
    m = _PROLOGUE.match(page)
    end = m.end() if m else 0
    return page[:end] + head + page[end:]


def _with_checker(page: str, script: str) -> str:
    if re.search(r"</body\s*>", page, re.I):
        return re.sub(r"(</body\s*>)", lambda m: script + m.group(1), page, count=1, flags=re.I)
    return page + script


def browser_argv(browser: str, size: tuple[int, int], extra: list[str] | None = None) -> list[str]:
    w, h = size
    return [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--mute-audio",
        # No network at all: a card is self-contained and can never fetch anything.
        "--host-resolver-rules=MAP * ~NOTFOUND",
        "--proxy-server=127.0.0.1:9",
        f"--window-size={w},{h}",
        f"--force-device-scale-factor={SCALE}",
        "--virtual-time-budget=5000",
        *(["--no-sandbox"] if _needs_no_sandbox() else []),
        *(extra or []),
    ]


def _needs_no_sandbox() -> bool:
    """Chromium refuses to start its sandbox as root on Linux (containers, CI)."""
    return sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0


def _run(argv: list[str], timeout: float, cwd: str) -> subprocess.CompletedProcess[str]:
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=cwd,
            check=False,
            **kwargs,
        )
    except subprocess.TimeoutExpired as exc:
        raise RenderError(f"the browser took longer than {timeout:.0f}s") from exc
    except OSError as exc:
        raise RenderError(f"could not start the browser: {exc}") from exc


def parse_report(dom: str) -> list[str] | None:
    """The checker's findings from a dumped DOM; None when the checker never reported."""
    m = re.search(rf'<pre id="{REPORT_ID}">(.*?)</pre>', dom, re.S)
    if not m:
        return None
    try:
        found = json.loads(html.unescape(m.group(1)))
    except json.JSONDecodeError:
        return None
    return [str(x) for x in found] if isinstance(found, list) else None


def render_card(
    html_path: Path,
    png_path: Path,
    *,
    browser: str,
    timeout: float = 60.0,
    extra_args: list[str] | None = None,
) -> RenderResult:
    """Check the layout and draw the card. Raises RenderError when no PNG could be made;
    layout problems are returned, not raised, so the session can fix them."""
    html_path = Path(html_path).resolve()
    png_path = Path(png_path).resolve()
    page = html_path.read_text(encoding="utf-8", errors="replace")
    if _REFRESH.search(html.unescape(page)):
        raise RenderError(
            'the card has a <meta http-equiv="refresh">; a card never navigates, so remove it'
        )
    size = card_size(page)
    css = font_css()
    nonce = secrets.token_hex(16)
    problems: list[str] = []
    if any(rx.search(page) for rx in _EXTERNAL):
        problems.append(
            "the card loads something from the web or another file (a font, image or "
            "script); cards are self-contained and the renderer blocks both, so inline it "
            "or drop it"
        )
    png_path.parent.mkdir(parents=True, exist_ok=True)
    # A draw that fails must not leave the last round's picture looking like this one.
    png_path.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="studio-card-") as tmp:
        tmpdir = Path(tmp)
        page_with_head = _with_head(page, css, nonce)
        shot_page = tmpdir / "card.html"
        shot_page.write_text(page_with_head, encoding="utf-8")
        check_page = tmpdir / "check.html"
        check_page.write_text(
            _with_checker(page_with_head, _checker_script(size, nonce)), encoding="utf-8"
        )
        argv = browser_argv(browser, size, extra_args) + [f"--user-data-dir={tmpdir / 'profile'}"]
        dumped = _run([*argv, "--dump-dom", check_page.as_uri()], timeout, tmp)
        report = parse_report(dumped.stdout or "")
        if report is None:
            problems.append(
                "the layout check did not run (the page may have a script error); fix the "
                "HTML so it loads cleanly"
            )
        else:
            problems += report
        shot = _run([*argv, f"--screenshot={png_path}", shot_page.as_uri()], timeout, tmp)
    if not png_path.is_file():
        reason = (shot.stderr or shot.stdout or "").strip().splitlines()[-1:] or ["no output"]
        raise RenderError(f"the browser made no picture (exit {shot.returncode}): {reason[0]}")
    return RenderResult(png=png_path, size=size, problems=problems)


def png_size(path: Path) -> tuple[int, int] | None:
    """Pixel size from a PNG header, or None when the file is not a PNG."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
