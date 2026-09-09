"""Item model, normalisation helpers, and the Source ABC."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from abc import ABC, abstractmethod
from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel, Field

_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]")
_TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "ref")


def utcnow() -> datetime:
    return datetime.now(UTC)


def normalize_title(title: str | None) -> str:
    """Lowercase, strip accents/punctuation, collapse whitespace."""
    if not title:
        return ""
    t = unicodedata.normalize("NFKD", title)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = _PUNCT_RE.sub(" ", t.lower())
    return _WS_RE.sub(" ", t).strip()


def normalize_url(url: str | None) -> str:
    """Lowercase scheme/host, drop fragment and tracking params, strip trailing slash."""
    if not url:
        return ""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith(_TRACKING_PARAMS)
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    m = _DOI_RE.search(doi)
    if not m:
        return None
    return m.group(0).lower().rstrip(".,;)")


def extract_doi(*texts: str | None) -> str | None:
    for t in texts:
        d = normalize_doi(t)
        if d:
            return d
    return None


def compute_dedup_hash(title: str | None, url: str | None) -> str:
    key = normalize_title(title) + "|" + normalize_url(url)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def make_item_id(source: str, title: str | None, url: str | None) -> str:
    return hashlib.sha256(f"{source}|{compute_dedup_hash(title, url)}".encode()).hexdigest()[:24]


class Item(BaseModel):
    id: str
    source: str
    url: str
    doi: str | None = None
    title: str
    abstract: str = ""
    published_at: datetime | None = None
    fetched_at: datetime = Field(default_factory=utcnow)
    dedup_hash: str
    cluster_id: int | None = None
    raw_json: str = "{}"

    @classmethod
    def build(
        cls,
        *,
        source: str,
        url: str,
        title: str,
        abstract: str = "",
        doi: str | None = None,
        published_at: datetime | None = None,
        raw: Any = None,
    ) -> Item:
        title = _WS_RE.sub(" ", (title or "")).strip()
        return cls(
            id=make_item_id(source, title, url),
            source=source,
            url=url,
            doi=normalize_doi(doi) if doi else extract_doi(url),
            title=title,
            abstract=(abstract or "").strip(),
            published_at=published_at,
            dedup_hash=compute_dedup_hash(title, url),
            raw_json=json.dumps(raw, default=str, ensure_ascii=False) if raw is not None else "{}",
        )


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        return date.fromisoformat(value.strip()[:10])
    return None


def parse_windows(raw: Any) -> list[tuple[date, date, int | None]]:
    """Normalise a `windows:` config list into (start, end, cadence_minutes|None).

    Malformed entries are skipped. Both days are inclusive.
    """
    out: list[tuple[date, date, int | None]] = []
    for w in raw or []:
        if not isinstance(w, dict):
            continue
        try:
            start, end = _as_date(w.get("start")), _as_date(w.get("end"))
        except ValueError:
            continue
        if start is None or end is None or end < start:
            continue
        cad = w.get("cadence_minutes")
        out.append((start, end, int(cad) if cad is not None else None))
    return out


class Source(ABC):
    """A configured source. Subclasses implement fetch(); network calls go through
    small, mockable methods so tests never touch the network.

    Meeting windows (step 6): a config may carry
        windows: [{start: YYYY-MM-DD, end: YYYY-MM-DD, cadence_minutes: N}]
    When the UTC date of `now` falls inside a window (inclusive) that window's
    cadence replaces `cadence_minutes`. No windows -> plain cadence.

    `enabled` pass-through: config.py copies `enabled: false` into every
    expanded source (company feeds, conference sources, the KOL list) so a
    health check can tell "deliberately off" from "never ran".
    """

    type: str = "base"

    def __init__(self, cfg: dict[str, Any], global_cfg: dict[str, Any] | None = None):
        self.cfg = cfg
        self.global_cfg = global_cfg or {}
        self.name: str = cfg["name"]
        self.cadence_minutes: int = int(cfg.get("cadence_minutes", 60))
        self.enabled: bool = bool(cfg.get("enabled", True))
        self.windows = parse_windows(cfg.get("windows"))

    @abstractmethod
    def fetch(self) -> list[Item]:
        """Return all currently available items (new or not); the caller dedups."""

    def active_window(self, now: datetime | None = None) -> tuple[date, date, int | None] | None:
        """The first configured window containing `now` (UTC date), if any."""
        if not self.windows:
            return None
        today = (now or utcnow()).astimezone(UTC).date()
        for w in self.windows:
            if w[0] <= today <= w[1]:
                return w
        return None

    def effective_cadence_minutes(self, now: datetime | None = None) -> int:
        w = self.active_window(now)
        if w is not None and w[2] is not None:
            return w[2]
        return self.cadence_minutes

    def is_due(self, last_run: datetime | None, now: datetime | None = None) -> bool:
        if not self.enabled:
            return False
        if last_run is None:
            return True
        now = now or utcnow()
        return (now - last_run).total_seconds() >= self.effective_cadence_minutes(now) * 60
