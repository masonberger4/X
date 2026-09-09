"""Generic RSS/Atom/RDF ingester (feedparser). Config:

- name: nature_medicine
  type: rss
  url: https://...
  cadence_minutes: 360
"""

from __future__ import annotations

import calendar
import html
import logging
import re
from datetime import UTC, datetime
from typing import Any

import feedparser

from ingest import http
from ingest.base import Item, Source, extract_doi

log = logging.getLogger(__name__)
_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", s))).strip()


def _entry_published(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        st = entry.get(key)
        if st:
            return datetime.fromtimestamp(calendar.timegm(st), tz=UTC)
    return None


def _entry_abstract(entry: Any) -> str:
    content = entry.get("content")
    if content:
        best = max((c.get("value") or "" for c in content), key=len)
        if best:
            return strip_html(best)
    return strip_html(entry.get("summary") or entry.get("description"))


def _entry_doi(entry: Any) -> str | None:
    # Deliberately not the summary: press releases cite papers' DOIs in body
    # text, which would merge unrelated releases into one cluster.
    return extract_doi(
        entry.get("prism_doi"),
        entry.get("dc_identifier"),
        entry.get("id"),
        entry.get("link"),
    )


def parse_feed(text: str, source_name: str) -> list[Item]:
    parsed = feedparser.parse(text)
    if not parsed.entries and (parsed.bozo or not parsed.get("version")):
        raise ValueError(
            f"{source_name}: unparseable feed ({parsed.get('bozo_exception') or 'not a feed'})"
        )
    items: list[Item] = []
    for e in parsed.entries:
        title = strip_html(e.get("title"))
        link = (e.get("link") or "").strip()
        if not title or not link:
            log.debug("%s: skipping entry without title/link", source_name)
            continue
        raw = {
            k: v
            for k, v in e.items()
            if isinstance(v, (str, int, float, list, dict))
            and not k.endswith("_parsed")
            and not k.endswith("_detail")
        }
        items.append(
            Item.build(
                source=source_name,
                url=link,
                title=title,
                abstract=_entry_abstract(e),
                doi=_entry_doi(e),
                published_at=_entry_published(e),
                raw=raw,
            )
        )
    return items


class RSSSource(Source):
    type = "rss"

    def fetch_text(self) -> str:
        return http.get_text(
            self.cfg["url"], user_agent=(self.global_cfg.get("http") or {}).get("user_agent")
        )

    def fetch(self) -> list[Item]:
        items = parse_feed(self.fetch_text(), self.name)
        log.info("%s: parsed %d entries", self.name, len(items))
        return items
