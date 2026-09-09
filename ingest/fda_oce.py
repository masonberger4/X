"""FDA Oncology Center of Excellence approval notifications (HTML scrape).

Isolated and fails soft: any parse problem logs a warning and returns []. The
page is a list of short paragraphs like
  "On September 3, 2026, the Food and Drug Administration approved X for ..."
each linking to a detail page under /drugs/resources-information-approved-drugs/.

Config:
  - name: fda_oce_approvals
    type: fda_oce
    url: https://www.fda.gov/drugs/resources-information-approved-drugs/oncology-cancer-hematologic-malignancies-approval-notifications
    max_items: 40
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin

from ingest import http
from ingest.base import Item, Source
from ingest.rss import strip_html

log = logging.getLogger(__name__)
_DATE_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{1,2}),\s+(\d{4})\b"
)
_LINK_PATH = "/drugs/resources-information-approved-drugs/"


class _Blocks(HTMLParser):
    """Collect (text, first_link) for each <li>/<p> block inside the page."""

    def __init__(self):
        super().__init__()
        self.blocks: list[tuple[str, str | None]] = []
        self._buf: list[str] = []
        self._link: str | None = None
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("li", "p"):
            self._depth += 1
            self._buf, self._link = [], None
        elif tag == "a" and self._depth:
            href = dict(attrs).get("href")
            if href and self._link is None and _LINK_PATH in href:
                self._link = href

    def handle_data(self, data):
        if self._depth:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag in ("li", "p") and self._depth:
            self._depth -= 1
            text = strip_html(" ".join(self._buf))
            if text:
                self.blocks.append((text, self._link))
            self._buf, self._link = [], None


def parse_oce_page(
    html_text: str, source_name: str, base_url: str, max_items: int = 40
) -> list[Item]:
    p = _Blocks()
    p.feed(html_text)
    items = []
    for text, link in p.blocks:
        m = _DATE_RE.search(text)
        if not m or "Food and Drug Administration" not in text and "FDA" not in text:
            continue
        if not link:
            continue
        try:
            published = datetime.strptime(" ".join(m.groups()), "%B %d %Y").replace(tzinfo=UTC)
        except ValueError:
            published = None
        url = urljoin(base_url, link)
        # Title = the sentence after the date, trimmed
        title = text[m.end() :].lstrip(" ,").split(". ")[0]
        title = re.sub(r"^the Food and Drug Administration ", "FDA ", title, flags=re.I)
        items.append(
            Item.build(
                source=source_name,
                url=url,
                title=title[:300],
                abstract=text,
                published_at=published,
                raw={"text": text},
            )
        )
        if len(items) >= max_items:
            break
    return items


class FDAOCESource(Source):
    type = "fda_oce"

    def fetch_html(self) -> str:
        return http.get_text(
            self.cfg["url"], user_agent=(self.global_cfg.get("http") or {}).get("user_agent")
        )

    def fetch(self) -> list[Item]:
        try:
            items = parse_oce_page(
                self.fetch_html(), self.name, self.cfg["url"], int(self.cfg.get("max_items", 40))
            )
        except Exception as exc:  # fail soft: this page is fragile and bot-protected
            log.warning("%s: fetch/parse failed, returning no items (%s)", self.name, exc)
            return []
        if not items:
            log.warning("%s: page parsed but no approvals found (layout change?)", self.name)
        log.info("%s: parsed %d approvals", self.name, len(items))
        return items
