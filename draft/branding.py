"""Company branding for table cells: a stock ticker and a logo, from config and local files.

Nothing here is fetched or guessed. Tickers come from `config.yaml` (`ticker:` on a
`companies.feeds` entry, or `branding.companies` for companies without a feed) and logos
are PNG files a human has placed in `branding.logos_dir` (default `assets/logos/`), named
by the company key. `brand_table` rewrites a fact-checked table's company cells to
"Name ($TICKER)" and tells the renderer which logo to draw in which cell; a company the
config does not know is left exactly as it was. Pure: no network, no database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from draft.chart import Table

DEFAULT_LOGOS_DIR = "assets/logos"
DEFAULT_COMPANY_COLUMNS = ("company", "sponsor", "developer", "partner", "owner", "acquirer")
LOGO_SUFFIXES = (".png",)
_TICKER_RE = re.compile(r"\(\$[A-Z.\-]{1,6}\)")
# Words that may follow a configured name without changing who it names. Anything else
# ("Merck KGaA", "Merck (via Harpoon)") is a different or ambiguous entity: no match.
_SUFFIX_WORDS = frozenset(
    "inc incorporated corp corporation co company ltd limited plc sa ag nv se llc "
    "holdings group pharmaceuticals pharma biosciences therapeutics".split()
)


@dataclass(frozen=True)
class Brand:
    key: str
    name: str
    ticker: str = ""  # "" for a private company
    aliases: tuple[str, ...] = ()
    logo: Path | None = None

    def label(self, text: str) -> str:
        """`text` with " ($TICKER)" appended when it does not already carry a ticker."""
        if not self.ticker or _TICKER_RE.search(text):
            return text
        return f"{text} (${self.ticker})"


@dataclass
class Branding:
    brands: list[Brand] = field(default_factory=list)
    company_columns: tuple[str, ...] = DEFAULT_COMPANY_COLUMNS

    def match(self, text: str) -> Brand | None:
        """The brand the cell names: the configured name or alias exactly, optionally
        preceded by "the" and followed only by corporate suffixes or the brand's own
        ticker. "Merck KGaA" or "Merck (via Harpoon)" match nothing: a wrong ticker is
        worse than none. Longest name wins when several fit."""
        hay = _norm(text)
        if hay.startswith("the "):
            hay = hay[4:]
        if not hay:
            return None
        best: tuple[int, Brand] | None = None
        for b in self.brands:
            allowed = _SUFFIX_WORDS | {b.ticker.lower()} if b.ticker else _SUFFIX_WORDS
            for cand in (b.name, *b.aliases):
                n = _norm(cand)
                if not n or not (hay == n or hay.startswith(n + " ")):
                    continue
                rest = hay[len(n) :].split()
                if all(w in allowed for w in rest) and (best is None or len(n) > best[0]):
                    best = (len(n), b)
        return best[1] if best else None

    def is_company_column(self, header: str) -> bool:
        h = _norm(header)
        return any(
            h == c or h.startswith(c + " ") or h.endswith(" " + c) for c in self.company_columns
        )


def _norm(text: str) -> str:
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def load_branding(root_cfg: dict[str, Any] | None, base_dir: str | Path | None = None) -> Branding:
    """Brands from the root config: every `companies.feeds` entry (ticker optional) plus
    `branding.companies`. Logos are looked up as `<logos_dir>/<key>.png` under `base_dir`
    (the repo root or the desktop data dir); a missing file simply means no logo."""
    cfg = root_cfg or {}
    section = cfg.get("branding") or {}
    logos_dir = Path(base_dir or ".") / str(section.get("logos_dir") or DEFAULT_LOGOS_DIR)
    columns = tuple(
        _norm(str(c)) for c in (section.get("company_columns") or DEFAULT_COMPANY_COLUMNS)
    )
    entries: list[dict[str, Any]] = []
    entries += [e for e in ((cfg.get("companies") or {}).get("feeds") or []) if isinstance(e, dict)]
    entries += [e for e in (section.get("companies") or []) if isinstance(e, dict)]
    brands: list[Brand] = []
    seen: set[str] = set()
    for e in entries:
        key = str(e.get("key") or "").strip()
        name = str(e.get("name") or "").strip()
        if not key or not name or key in seen:
            continue
        seen.add(key)
        logo = None
        for suffix in LOGO_SUFFIXES:
            candidate = logos_dir / f"{key}{suffix}"
            if candidate.is_file():
                logo = candidate
                break
        aliases = tuple(str(a).strip() for a in (e.get("aliases") or []) if str(a).strip())
        brands.append(
            Brand(
                key=key,
                name=name,
                ticker=str(e.get("ticker") or "").strip().upper().lstrip("$"),
                aliases=aliases,
                logo=logo,
            )
        )
    return Branding(brands=brands, company_columns=columns)


def brand_table(table: Table, branding: Branding) -> tuple[Table, dict[tuple[int, int], Path]]:
    """The table with tickers appended in company columns, and the logo to draw per cell.
    Cells the config does not recognise are untouched, so nothing new is ever asserted:
    a ticker only appears next to a name the human wrote into config.yaml."""
    company_cols = [c for c, h in enumerate(table.columns) if branding.is_company_column(h)]
    if not company_cols:
        return table, {}
    rows = [list(r) for r in table.rows]
    logos: dict[tuple[int, int], Path] = {}
    for r, row in enumerate(rows):
        for c in company_cols:
            brand = branding.match(row[c]) if row[c].strip() else None
            if brand is None:
                continue
            row[c] = brand.label(row[c])
            if brand.logo is not None:
                logos[(r, c)] = brand.logo
    branded = Table(title=table.title, columns=list(table.columns), rows=rows, note=table.note)
    return branded, logos


__all__ = ["Brand", "Branding", "brand_table", "load_branding"]
