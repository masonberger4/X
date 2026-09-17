"""bioRxiv / medRxiv via the biorxiv.org JSON API (the RSS feeds were retired).

api.biorxiv.org has been answering 200 with an empty body (Sept 2026); the same
`/details/<server>/...` paths on api.medrxiv.org serve both servers, so that is
the host in config.yaml. The API ignores a `category` query param, so the
category is filtered here.

Config:
  - name: biorxiv_cancer_biology
    type: biorxiv
    server: biorxiv            # or medrxiv
    api_url: https://api.medrxiv.org/details
    category: cancer_biology   # optional, filtered in code
    lookback_days: 2
    max_pages: 8
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from ingest import http
from ingest.base import Item, Source, utcnow

log = logging.getLogger(__name__)
PAGE = 100  # the API returns 100 records per cursor page


def norm_category(value: str) -> str:
    """Categories come back as "cancer biology"; config writes "cancer_biology"."""
    return value.replace("_", " ").strip().lower()


def parse_collection(
    records: list[dict[str, Any]],
    source_name: str,
    server: str,
    category: str | None = None,
) -> list[Item]:
    wanted = norm_category(category) if category else None
    items = []
    for rec in records:
        doi = rec.get("doi")
        title = rec.get("title")
        if not doi or not title:
            continue
        if wanted and norm_category(rec.get("category") or "") != wanted:
            continue
        url = f"https://www.{server}.org/content/{doi}v{rec.get('version', '1')}"
        published = None
        if rec.get("date"):
            published = datetime.strptime(rec["date"], "%Y-%m-%d").replace(tzinfo=UTC)
        items.append(
            Item.build(
                source=source_name,
                url=url,
                title=title,
                abstract=rec.get("abstract") or "",
                doi=doi,
                published_at=published,
                raw=rec,
            )
        )
    return items


class BiorxivSource(Source):
    type = "biorxiv"

    def fetch_page(self, start: str, end: str, cursor: int) -> dict[str, Any]:
        server = self.cfg.get("server", "biorxiv")
        url = f"{self.cfg['api_url'].rstrip('/')}/{server}/{start}/{end}/{cursor}"
        return http.get_json(url, user_agent=self.user_agent())

    def fetch(self) -> list[Item]:
        server = self.cfg.get("server", "biorxiv")
        end = utcnow().date()
        start = end - timedelta(days=int(self.cfg.get("lookback_days", 2)))
        items: list[Item] = []
        cursor = 0
        category = self.cfg.get("category")
        for _ in range(int(self.cfg.get("max_pages", 5))):
            data = self.fetch_page(start.isoformat(), end.isoformat(), cursor)
            msg = (data.get("messages") or [{}])[0]
            if msg.get("status") != "ok":
                log.warning("%s: api status %s", self.name, msg)
                break
            coll = data.get("collection") or []
            items.extend(parse_collection(coll, self.name, server, category))
            total = int(msg.get("total") or 0)
            cursor += len(coll)
            if not coll or cursor >= total:
                break
        log.info(
            "%s: parsed %d preprints (%s..%s, category=%s)",
            self.name,
            len(items),
            start,
            end,
            category or "all",
        )
        return items
