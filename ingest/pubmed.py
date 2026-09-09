"""PubMed via Entrez esearch/efetch (biopython). Config:

  - name: pubmed_oncology
    type: pubmed
    query: (neoplasms[MeSH Major Topic]) AND clinical trial[pt]
    lookback_days: 2
    max_results: 100
    cadence_minutes: 180

NCBI etiquette: Entrez.email/tool from config; rate limited to 3 req/s
without NCBI_API_KEY and 10 req/s with one.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from Bio import Entrez

from ingest.base import Item, Source, utcnow

log = logging.getLogger(__name__)

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1
    )
}


class RateLimiter:
    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._last + self.interval - now
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()


_limiter: RateLimiter | None = None


def configure_entrez(ncbi_cfg: dict[str, Any]) -> RateLimiter:
    """Set Entrez.email/tool/api_key from config + env and return the rate limiter."""
    global _limiter
    Entrez.email = os.environ.get("NCBI_EMAIL") or ncbi_cfg.get("email")
    Entrez.tool = ncbi_cfg.get("tool", "cancer-news-pipeline")
    key = os.environ.get("NCBI_API_KEY") or None
    Entrez.api_key = key
    if _limiter is None:
        _limiter = RateLimiter(10.0 if key else 3.0)
    return _limiter


def _text(node: Any) -> str:
    return str(node) if node is not None else ""


def _pub_date(article: dict[str, Any]) -> datetime | None:
    # Prefer ArticleDate (electronic), then JournalIssue PubDate
    for ad in article.get("ArticleDate") or []:
        try:
            return datetime(int(ad["Year"]), int(ad["Month"]), int(ad["Day"]), tzinfo=UTC)
        except (KeyError, ValueError):
            pass
    pd = (article.get("Journal") or {}).get("JournalIssue", {}).get("PubDate", {})
    try:
        y = int(pd["Year"])
        m = pd.get("Month", "Jan")
        m = int(m) if str(m).isdigit() else _MONTHS.get(str(m)[:3], 1)
        d = int(pd.get("Day", 1))
        return datetime(y, m, d, tzinfo=UTC)
    except (KeyError, ValueError, TypeError):
        return None


def parse_efetch(records: dict[str, Any], source_name: str) -> list[Item]:
    """`records` is the result of Entrez.read() on an efetch XML handle."""
    items = []
    for pa in records.get("PubmedArticle", []):
        cit = pa["MedlineCitation"]
        art = cit["Article"]
        pmid = _text(cit["PMID"])
        title = _text(art.get("ArticleTitle"))
        doi = None
        for eid in art.get("ELocationID", []):
            if eid.attributes.get("EIdType") == "doi":
                doi = _text(eid)
                break
        if doi is None:
            for aid in pa.get("PubmedData", {}).get("ArticleIdList", []):
                if aid.attributes.get("IdType") == "doi":
                    doi = _text(aid)
                    break
        abstract_parts = (art.get("Abstract") or {}).get("AbstractText") or []
        chunks = []
        for p in abstract_parts:
            label = getattr(p, "attributes", {}).get("Label")
            chunks.append(f"{label}: {p}" if label else _text(p))
        abstract = " ".join(chunks).strip()
        journal = _text((art.get("Journal") or {}).get("Title"))
        raw = {
            "pmid": pmid,
            "journal": journal,
            "doi": doi,
            "pub_types": [_text(t) for t in art.get("PublicationTypeList", [])],
        }
        items.append(
            Item.build(
                source=source_name,
                url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                title=title,
                abstract=abstract,
                doi=doi,
                published_at=_pub_date(art),
                raw=raw,
            )
        )
    return items


class PubMedSource(Source):
    type = "pubmed"

    def __init__(self, cfg, global_cfg=None):
        super().__init__(cfg, global_cfg)
        self.limiter = configure_entrez(self.global_cfg.get("ncbi") or {})

    # ---- network (mocked in tests) ----
    def esearch(self, term: str, mindate: str, maxdate: str, retmax: int) -> list[str]:
        self.limiter.wait()
        h = Entrez.esearch(
            db="pubmed",
            term=term,
            retmax=retmax,
            datetype="edat",
            mindate=mindate,
            maxdate=maxdate,
            sort="date",
        )
        try:
            return list(Entrez.read(h)["IdList"])
        finally:
            h.close()

    def efetch(self, ids: list[str]) -> dict[str, Any]:
        self.limiter.wait()
        h = Entrez.efetch(db="pubmed", id=",".join(ids), rettype="xml", retmode="xml")
        try:
            return Entrez.read(h)
        finally:
            h.close()

    def fetch(self) -> list[Item]:
        end = utcnow()
        start = end - timedelta(days=int(self.cfg.get("lookback_days", 2)))
        ids = self.esearch(
            self.cfg["query"],
            start.strftime("%Y/%m/%d"),
            end.strftime("%Y/%m/%d"),
            int(self.cfg.get("max_results", 100)),
        )
        items: list[Item] = []
        for i in range(0, len(ids), 50):
            items.extend(parse_efetch(self.efetch(ids[i : i + 50]), self.name))
        log.info("%s: %d pmids, %d parsed", self.name, len(ids), len(items))
        return items
