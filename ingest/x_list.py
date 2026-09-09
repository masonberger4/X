"""Curated X list of oncology KOLs, read-only via the X API v2 (step 6).

Reads recent posts from ONE list (created by hand on X) with app-only auth:
GET <api_url>/lists/<list_id>/tweets. Disabled by default; needs
X_BEARER_TOKEN (shared with step 4's feedback loop) and X_KOL_LIST_ID.
Nothing here writes to X. Config (expanded by config.py from `kol`):

  - name: kol_x_list
    type: x_list
    enabled: false
    api_url: https://api.x.com/2
    list_id: ""                 # X_KOL_LIST_ID env overrides
    cadence_minutes: 120
    max_results: 100
    max_pages: 1
    lookback_hours: 6           # must exceed cadence so runs overlap
    require_link: true
    skip_retweets: true
    skip_replies: true
    enforce_handles: false
    monthly_request_cap: 500
    handles: [{handle, name, focus}, ...]

Network only through fetch_page(params, token) -> http.get_json. The token
is passed as a header and never logged.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from ingest import http
from ingest.base import Item, Source, extract_doi, utcnow

log = logging.getLogger(__name__)

TWEET_FIELDS = "created_at,public_metrics,entities,referenced_tweets,author_id"
EXPANSIONS = "author_id"
USER_FIELDS = "username,name"
INTERNAL_HOSTS = ("x.com", "twitter.com", "t.co")
TITLE_CHARS = 120
_WS_RE = re.compile(r"\s+")


def _is_internal(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in INTERNAL_HOSTS)


def _parse_created(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def estimated_requests_per_month(cfg: dict[str, Any]) -> int:
    """runs/month x pages per run, for the monthly cap check (pure)."""
    cadence = max(1, int(cfg.get("cadence_minutes", 120)))
    runs = (30 * 24 * 60) // cadence
    return runs * max(1, int(cfg.get("max_pages", 1)))


def parse_tweets(payload: dict[str, Any], source_name: str, cfg: dict[str, Any]) -> list[Item]:
    return parse_tweets_stats(payload, source_name, cfg)[0]


def parse_tweets_stats(
    payload: dict[str, Any], source_name: str, cfg: dict[str, Any]
) -> tuple[list[Item], dict[str, int]]:
    """Pure: /2/lists/:id/tweets payload -> Items plus skip counts by reason."""
    users = {
        u.get("id"): u
        for u in ((payload.get("includes") or {}).get("users") or [])
        if isinstance(u, dict)
    }
    handles = {
        str(h.get("handle") if isinstance(h, dict) else h).lstrip("@").lower()
        for h in cfg.get("handles") or []
        if h
    }
    skip_rt = bool(cfg.get("skip_retweets", True))
    skip_reply = bool(cfg.get("skip_replies", True))
    require_link = bool(cfg.get("require_link", True))
    enforce = bool(cfg.get("enforce_handles", False))
    stats = {"fetched": 0, "retweet": 0, "reply": 0, "no_link": 0, "unknown_author": 0}
    stats.update(malformed=0, kept=0)
    items: list[Item] = []
    for tw in payload.get("data") or []:
        stats["fetched"] += 1
        if not isinstance(tw, dict) or not tw.get("id") or not tw.get("text"):
            stats["malformed"] += 1
            log.debug("%s: skipping malformed tweet record", source_name)
            continue
        refs = [r for r in tw.get("referenced_tweets") or [] if isinstance(r, dict)]
        types = {r.get("type") for r in refs}
        if skip_rt and "retweeted" in types:
            stats["retweet"] += 1
            continue
        if skip_reply and "replied_to" in types:
            stats["reply"] += 1
            continue
        user = users.get(tw.get("author_id")) or {}
        username = str(user.get("username") or "").lstrip("@")
        if enforce and username.lower() not in handles:
            stats["unknown_author"] += 1
            log.debug("%s: author @%s not in kol.handles", source_name, username or "?")
            continue
        text = str(tw["text"])
        links: list[str] = []
        for u in (tw.get("entities") or {}).get("urls") or []:
            if not isinstance(u, dict):
                continue
            short, expanded = u.get("url"), u.get("expanded_url") or u.get("unwound_url")
            if short and expanded:
                text = text.replace(short, expanded)
            if expanded and not _is_internal(expanded) and expanded not in links:
                links.append(expanded)
        if require_link and not links:
            stats["no_link"] += 1
            continue
        text = _WS_RE.sub(" ", text).strip()
        if not username:
            log.debug("%s: tweet %s has no author in includes", source_name, tw["id"])
        author = username or str(tw.get("author_id") or "unknown")
        abstract = text
        if links:
            abstract += "\nLinks: " + " ".join(links)
        raw = {
            "author": {"id": tw.get("author_id"), "username": username, "name": user.get("name")},
            "metrics": tw.get("public_metrics") or {},
            "links": links,
            "referenced_tweets": refs,
        }
        items.append(
            Item.build(
                source=source_name,
                url=f"https://x.com/{author}/status/{tw['id']}",
                title=f"@{author}: {text[:TITLE_CHARS]}",
                abstract=abstract,
                doi=extract_doi(*links),
                published_at=_parse_created(tw.get("created_at")),
                raw=raw,
            )
        )
        stats["kept"] += 1
    return items, stats


class XListSource(Source):
    type = "x_list"

    def list_id(self) -> str:
        return (os.environ.get("X_KOL_LIST_ID") or str(self.cfg.get("list_id") or "")).strip()

    @staticmethod
    def token() -> str:
        return (os.environ.get("X_BEARER_TOKEN") or "").strip()

    def build_params(self, pagination_token: str | None = None) -> dict[str, Any]:
        lookback = int(self.cfg.get("lookback_hours", 6))
        start = (utcnow() - timedelta(hours=lookback)).replace(microsecond=0)
        params: dict[str, Any] = {
            "tweet.fields": TWEET_FIELDS,
            "expansions": EXPANSIONS,
            "user.fields": USER_FIELDS,
            "max_results": min(100, max(5, int(self.cfg.get("max_results", 100)))),
            "start_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if pagination_token:
            params["pagination_token"] = pagination_token
        return params

    def fetch_page(self, params: dict[str, Any], token: str) -> dict[str, Any]:
        url = f"{str(self.cfg.get('api_url', 'https://api.x.com/2')).rstrip('/')}"
        url = f"{url}/lists/{self.list_id()}/tweets"
        return http.get_json(
            url,
            params=params,
            user_agent=(self.global_cfg.get("http") or {}).get("user_agent"),
            headers={"Authorization": f"Bearer {token}"},
        )

    def fetch(self) -> list[Item]:
        token = self.token()
        list_id = self.list_id()
        if not token:
            raise RuntimeError(
                f"{self.name}: X_BEARER_TOKEN is not set in .env; the KOL list source needs "
                "X API read access (or set kol.enabled: false)"
            )
        if not list_id:
            raise RuntimeError(
                f"{self.name}: no list id; set X_KOL_LIST_ID in .env or kol.list_id in config.yaml"
            )
        est = estimated_requests_per_month(self.cfg)
        cap = int(self.cfg.get("monthly_request_cap", 0) or 0)
        log.info("%s: estimated %d list requests/month (cap %d)", self.name, est, cap)
        if cap and est > cap:
            log.warning("%s: estimate exceeds kol.monthly_request_cap", self.name)
        items: list[Item] = []
        totals: dict[str, int] = {}
        next_token = None
        for _ in range(max(1, int(self.cfg.get("max_pages", 1)))):
            payload = self.fetch_page(self.build_params(next_token), token)
            page_items, stats = parse_tweets_stats(payload or {}, self.name, self.cfg)
            items.extend(page_items)
            for k, v in stats.items():
                totals[k] = totals.get(k, 0) + v
            next_token = ((payload or {}).get("meta") or {}).get("next_token")
            if not next_token or not (payload or {}).get("data"):
                break
        log.info("%s: tweets %s", self.name, totals)
        return items
