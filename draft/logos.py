"""Find and normalise a company's own site icon for `assets/logos/` (run_logos.py).

Pure helpers: which site to look at (`company_domain`), which icon a homepage advertises
(`icon_candidates` parses the `<link rel="apple-touch-icon"|"icon">` tags and orders them
by declared size), and how to turn whatever came back into a small RGBA PNG
(`normalise_png`, Pillow, which matplotlib already depends on). The network calls live in
run_logos.py through `ingest/http.py`. Nothing here guesses a logo: the only images ever
considered are the ones the company's own homepage links as its icon, or the well-known
`/apple-touch-icon.png` and `/favicon.ico` paths on that same host.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

# IR-platform hosts: a feed there says nothing about the company's own domain.
HOSTED_FEED_DOMAINS = frozenset(
    {
        "gcs-web.com",
        "q4cdn.com",
        "q4web.com",
        "globenewswire.com",
        "businesswire.com",
        "prnewswire.com",
    }
)
_STRIP_SUBDOMAINS = ("www.", "ir.", "investor.", "investors.", "media.", "news.")
WELL_KNOWN_PATHS = ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png", "/favicon.ico")
MAX_SIDE = 256  # the stored PNG's longest side; a table row needs ~60 px


@dataclass(frozen=True)
class IconCandidate:
    url: str
    rel: str
    size: int  # declared pixel size (0 when unknown)

    @property
    def rank(self) -> tuple[int, int]:
        """apple-touch-icon first (a real logo mark, not a 16 px favicon), then larger."""
        return (1 if "apple-touch-icon" in self.rel else 0, self.size)


def company_domain(entry: dict[str, Any]) -> str | None:
    """`domain:` when set, else the host of the feed URL minus ir./investors./www.; None
    when the feed lives on an IR platform (then the config needs an explicit domain)."""
    explicit = str(entry.get("domain") or "").strip().lower()
    if explicit:
        return explicit.removeprefix("https://").removeprefix("http://").strip("/")
    url = str(entry.get("url") or "")
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return None
    for platform in HOSTED_FEED_DOMAINS:
        if host == platform or host.endswith("." + platform):
            return None
    for prefix in _STRIP_SUBDOMAINS:
        if host.startswith(prefix):
            host = host[len(prefix) :]
            break
    return host or None


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "link":
            self.links.append({k.lower(): (v or "") for k, v in attrs})


def _declared_size(sizes: str) -> int:
    m = re.search(r"(\d+)\s*x\s*(\d+)", sizes or "", re.IGNORECASE)
    return max(int(m.group(1)), int(m.group(2))) if m else 0


def icon_candidates(html: str, base_url: str) -> list[IconCandidate]:
    """Icons the page links, best first: apple-touch-icons by size, then plain icons by
    size. SVG icons are skipped (no rasteriser in the project); the well-known paths are
    appended last so every host gets at least those two tries."""
    parser = _LinkParser()
    try:
        parser.feed(html)
    except Exception:  # a broken page still gets the well-known paths
        pass
    found: list[IconCandidate] = []
    for link in parser.links:
        rel = link.get("rel", "").lower()
        href = link.get("href", "").strip()
        if not href or not any(t in rel.split() for t in ("icon", "apple-touch-icon")):
            continue
        if "mask-icon" in rel or href.lower().split("?")[0].endswith(".svg"):
            continue
        if "svg" in link.get("type", "").lower():
            continue
        found.append(
            IconCandidate(urljoin(base_url, href), rel, _declared_size(link.get("sizes", "")))
        )
    found.sort(key=lambda c: c.rank, reverse=True)
    seen = {c.url for c in found}
    for path in WELL_KNOWN_PATHS:
        url = urljoin(base_url, path)
        if url not in seen:
            found.append(IconCandidate(url, "well-known", 0))
    return found


def normalise_png(data: bytes, content_type: str = "") -> bytes | None:
    """`data` (PNG, ICO, JPEG, WebP, GIF) as an RGBA PNG no larger than MAX_SIDE on its
    longest side; the largest frame of an ICO. None when Pillow cannot read it, when it
    is an HTML error page, or when it is tiny (a 16 px favicon is not a logo)."""
    if (
        "html" in content_type.lower()
        or data[:15].lstrip().lower().startswith(b"<!doctype html")
        or data[:6].lower() == b"<html>"
    ):
        return None
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(data))
        if getattr(img, "format", "") == "ICO":
            best = max(img.ico.sizes(), key=lambda s: s[0] * s[1])  # type: ignore[attr-defined]
            img = img.ico.getimage(best)  # type: ignore[attr-defined]
        img = img.convert("RGBA")
    except Exception:
        return None
    if max(img.size) < 32:
        return None
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


__all__ = [
    "HOSTED_FEED_DOMAINS",
    "IconCandidate",
    "MAX_SIDE",
    "WELL_KNOWN_PATHS",
    "company_domain",
    "icon_candidates",
    "normalise_png",
]
