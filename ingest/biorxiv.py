"""bioRxiv / medRxiv via the api.biorxiv.org JSON API (the RSS feeds were retired).

Config:
  - name: biorxiv_cancer_biology
    type: biorxiv
    server: biorxiv            # or medrxiv
    api_url: https://api.biorxiv.org/details
    category: cancer_biology   # optional
    lookback_days: 2
    max_pages: 5
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ingest import http
from ingest.base import Item, Source, utcnow

log = logging.getLogger(__name__)
PAGE = 100  # the API returns 100 records per cursor page


def parse_collection(records: list[dict[str, Any]], source_name: str, server: str) -> list[Item]:
    items = []
    for rec in records:
        doi = rec.get("doi")
        title = rec.get("title")
        if not doi or not title:
            continue
        url = f"https://www.{server}.org/content/{doi}v{rec.get('version', '1')}"
        published = None
        if rec.get("date"):
            published = datetime.strptime(rec["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        items.append(Item.build(source=source_name, url=url, title=title,
                                abstract=rec.get("abstract") or "", doi=doi,
                                published_at=published, raw=rec))
    return items


class BiorxivSource(Source):
    type = "biorxiv"

    def fetch_page(self, start: str, end: str, cursor: int) -> dict[str, Any]:
        server = self.cfg.get("server", "biorxiv")
        url = f"{self.cfg['api_url'].rstrip('/')}/{server}/{start}/{end}/{cursor}"
        params = {"category": self.cfg["category"]} if self.cfg.get("category") else None
        return http.get_json(url, params=params,
                             user_agent=(self.global_cfg.get("http") or {}).get("user_agent"))

    def fetch(self) -> list[Item]:
        server = self.cfg.get("server", "biorxiv")
        end = utcnow().date()
        start = end - timedelta(days=int(self.cfg.get("lookback_days", 2)))
        items: list[Item] = []
        cursor = 0
        for _ in range(int(self.cfg.get("max_pages", 5))):
            data = self.fetch_page(start.isoformat(), end.isoformat(), cursor)
            msg = (data.get("messages") or [{}])[0]
            if msg.get("status") != "ok":
                log.warning("%s: api status %s", self.name, msg)
                break
            coll = data.get("collection") or []
            items.extend(parse_collection(coll, self.name, server))
            total = int(msg.get("total") or 0)
            cursor += len(coll)
            if not coll or cursor >= total:
                break
        log.info("%s: parsed %d preprints (%s..%s)", self.name, len(items), start, end)
        return items
