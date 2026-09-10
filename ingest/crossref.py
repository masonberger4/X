"""Conference-abstract supplements via the Crossref REST API (step 6).

Societies publish their meeting abstracts as a journal supplement (JCO for
ASCO, Cancer Research for AACR, Blood for ASH, Annals of Oncology for ESMO).
Crossref indexes those supplements with the same ISSN as the journal, so the
source filters by ISSN + created date and keeps only works whose `issue` (or
DOI) matches `issue_pattern`. The remaining flood is tamed inside the source:
a keyword gate, priority ordering (late-breaking / plenary first, then newest)
and a per-run cap. Config (expanded by config.py from `conferences.meetings`):

  - name: conf_asco_abstracts
    type: crossref
    meeting: "ASCO Annual Meeting"
    journal: "Journal of Clinical Oncology"
    issn: 0732-183X
    issue_pattern: suppl          # regex, case-insensitive, against issue then DOI
    boost_patterns: [LBA, late-breaking, plenary]
    keywords: [...]               # optional; defaults to prefilter.allow_keywords
    lookback_days: 3
    rows: 100
    max_pages: 5
    max_items_per_run: 40
    cadence_minutes: 1440
    windows: [{start: ..., end: ..., cadence_minutes: 60}]

Network only through fetch_page(params) -> http.get_json.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from ingest import http
from ingest.base import Item, Source, utcnow
from ingest.rss import strip_html

log = logging.getLogger(__name__)

DEFAULT_API_URL = "https://api.crossref.org/works"
SELECT_FIELDS = "DOI,URL,title,abstract,created,issued,container-title,volume,issue,type,event"


def _first(value: Any) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value) if value else ""


def _date_parts(block: Any) -> datetime | None:
    try:
        parts = block["date-parts"][0]
        y, m, d = (list(parts) + [1, 1])[:3]
        return datetime(int(y), int(m or 1), int(d or 1), tzinfo=UTC)
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _created(work: dict[str, Any]) -> datetime | None:
    created = work.get("created") or {}
    dt = created.get("date-time")
    if dt:
        try:
            return datetime.fromisoformat(str(dt).replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            pass
    return _date_parts(created) or _date_parts(work.get("issued") or {})


def _compile(pattern: str | None) -> re.Pattern[str] | None:
    return re.compile(pattern, re.IGNORECASE) if pattern else None


def _keywords(cfg: dict[str, Any], global_cfg: dict[str, Any] | None) -> list[str]:
    kws = cfg.get("keywords")
    if kws is None:
        kws = ((global_cfg or {}).get("prefilter") or {}).get("allow_keywords") or []
    return [str(k).lower() for k in kws if k]


def meeting_line(work: dict[str, Any], cfg: dict[str, Any]) -> str:
    """One factual line prepended to the abstract so the scorer sees the meeting context."""
    meeting = cfg.get("meeting") or cfg.get("label") or cfg.get("name", "")
    when = _date_parts(work.get("issued") or {}) or _created(work)
    year = f" {when.year}" if when else ""
    journal = _first(work.get("container-title")) or cfg.get("journal") or ""
    vol, issue = _first(work.get("volume")), _first(work.get("issue"))
    where = ", ".join(p for p in (f"{journal} {vol}".strip(), issue) if p)
    return f"{meeting}{year} abstract ({where})." if where else f"{meeting}{year} abstract."


def parse_works(
    works: list[dict[str, Any]],
    source_name: str,
    cfg: dict[str, Any],
    global_cfg: dict[str, Any] | None = None,
) -> list[Item]:
    """Pure: Crossref work records -> Items, in priority order, capped.

    `parse_works_stats` also returns the per-stage counts.
    """
    return parse_works_stats(works, source_name, cfg, global_cfg)[0]


def parse_works_stats(
    works: list[dict[str, Any]],
    source_name: str,
    cfg: dict[str, Any],
    global_cfg: dict[str, Any] | None = None,
) -> tuple[list[Item], dict[str, int]]:
    issue_re = _compile(cfg.get("issue_pattern"))
    boost_re = _compile("|".join(re.escape(str(p)) for p in cfg.get("boost_patterns") or [] if p))
    keywords = _keywords(cfg, global_cfg)
    cap = int(cfg.get("max_items_per_run", 0) or 0)
    stats = {"fetched": len(works), "issue": 0, "keywords": 0, "kept": 0}

    scored: list[tuple[int, datetime, Item]] = []
    for w in works:
        if not isinstance(w, dict):
            log.debug("%s: skipping non-dict record", source_name)
            continue
        doi = str(w.get("DOI") or "").strip()
        title = strip_html(_first(w.get("title")))
        if not doi or not title:
            log.debug("%s: skipping record without DOI/title", source_name)
            continue
        issue = _first(w.get("issue"))
        if issue_re and not (issue_re.search(issue) or issue_re.search(doi)):
            log.debug("%s: issue %r does not match: %s", source_name, issue, doi)
            continue
        stats["issue"] += 1
        abstract = strip_html(w.get("abstract"))
        text = f"{title}\n{abstract}".lower()
        if keywords and not any(k in text for k in keywords):
            log.debug("%s: no keyword: %s", source_name, title[:80])
            continue
        stats["keywords"] += 1
        created = _created(w) or datetime.min.replace(tzinfo=UTC)
        boosted = bool(boost_re and (boost_re.search(doi) or boost_re.search(title)))
        raw = {
            "container-title": _first(w.get("container-title")),
            "volume": _first(w.get("volume")),
            "issue": issue,
            "type": w.get("type"),
            "meeting": cfg.get("meeting") or cfg.get("label"),
            "session_hint": _first(w.get("event")) or ("late-breaking" if boosted else None),
        }
        item = Item.build(
            source=source_name,
            url=f"https://doi.org/{doi}",
            title=title,
            abstract=f"{meeting_line(w, cfg)} {abstract}".strip(),
            doi=doi,
            published_at=_created(w),
            raw=raw,
        )
        scored.append((int(boosted), created, item))

    # boosted first, then newest created.
    scored.sort(key=lambda t: (-t[0], -t[1].timestamp()))
    items = [t[2] for t in scored]
    if cap and len(items) > cap:
        items = items[:cap]
    stats["kept"] = len(items)
    return items, stats


class CrossrefSource(Source):
    type = "crossref"

    def _crossref_cfg(self) -> dict[str, Any]:
        return self.global_cfg.get("crossref") or {}

    def mailto(self) -> str | None:
        return os.environ.get("CROSSREF_MAILTO") or self._crossref_cfg().get("mailto") or None

    def build_params(self, cursor: str = "*") -> dict[str, Any]:
        lookback = int(self.cfg.get("lookback_days", self._crossref_cfg().get("lookback_days", 3)))
        since = (utcnow() - timedelta(days=lookback)).date().isoformat()
        params: dict[str, Any] = {
            "filter": f"issn:{self.cfg['issn']},from-created-date:{since}",
            "rows": int(self.cfg.get("rows", self._crossref_cfg().get("rows", 100))),
            "select": SELECT_FIELDS,
            "sort": "created",
            "order": "desc",
            "cursor": cursor,
        }
        mailto = self.mailto()
        if mailto:
            params["mailto"] = mailto
        return params

    def fetch_page(self, params: dict[str, Any]) -> dict[str, Any]:
        url = self.cfg.get("api_url") or self._crossref_cfg().get("api_url") or DEFAULT_API_URL
        return http.get_json(url, params=params, user_agent=self.user_agent())

    def fetch(self) -> list[Item]:
        works: list[dict[str, Any]] = []
        cursor = "*"
        max_pages = int(self.cfg.get("max_pages", self._crossref_cfg().get("max_pages", 5)))
        for _ in range(max_pages):
            data = self.fetch_page(self.build_params(cursor))
            msg = (data or {}).get("message") or {}
            page = msg.get("items") or []
            works.extend(page)
            cursor = msg.get("next-cursor")
            if not page or not cursor:
                break
        items, stats = parse_works_stats(works, self.name, self.cfg, self.global_cfg)
        log.info(
            "%s: works fetched=%d kept_after_issue=%d kept_after_keywords=%d capped_to=%d",
            self.name,
            stats["fetched"],
            stats["issue"],
            stats["keywords"],
            stats["kept"],
        )
        return items
