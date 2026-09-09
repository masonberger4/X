"""XListSource (step 6): synthetic /2/lists/:id/tweets payload (the endpoint needs a token)."""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from config import load_config
from ingest.x_list import (
    XListSource,
    estimated_requests_per_month,
    parse_tweets,
    parse_tweets_stats,
)

FIX = Path(__file__).parent / "fixtures"
GLOBAL = {"http": {"user_agent": "test"}}
CFG = {
    "name": "kol_x_list",
    "type": "x_list",
    "enabled": True,
    "api_url": "https://api.x.com/2",
    "list_id": "",
    "cadence_minutes": 120,
    "max_results": 100,
    "max_pages": 2,
    "lookback_hours": 6,
    "require_link": True,
    "skip_retweets": True,
    "skip_replies": True,
    "enforce_handles": False,
    "monthly_request_cap": 500,
    "handles": [
        {"handle": "montypal", "name": "Sumanta Pal", "focus": "GU oncology"},
        {"handle": "@RahulBanerjeeMD", "name": "Rahul Banerjee", "focus": "myeloma, CAR-T"},
        {"handle": "FDAOncology", "name": "FDA OCE", "focus": "approvals"},
        {"handle": "adamfeuerstein", "name": "Adam Feuerstein", "focus": "biotech"},
    ],
}


def _payload():
    return json.loads((FIX / "x_list_tweets.json").read_text())


def test_parse_skips_retweet_reply_and_x_only_link():
    items, stats = parse_tweets_stats(_payload(), "kol_x_list", CFG)
    ids = [i.url.rsplit("/", 1)[1] for i in items]
    assert ids == ["1830000000000000001", "1830000000000000002", "1830000000000000006"]
    assert stats == {
        "fetched": 7,
        "retweet": 1,
        "reply": 1,
        "no_link": 2,  # the x.com-only quote and the link-less FDA post
        "unknown_author": 0,
        "malformed": 0,
        "kept": 3,
    }


def test_enforce_handles_drops_unknown_author():
    items = parse_tweets(_payload(), "kol_x_list", dict(CFG, enforce_handles=True))
    assert [json.loads(i.raw_json)["author"]["username"] for i in items] == [
        "montypal",
        "RahulBanerjeeMD",
    ]


def test_require_link_false_keeps_link_less_post():
    items = parse_tweets(_payload(), "kol_x_list", dict(CFG, require_link=False))
    assert len(items) == 5  # the x.com-only quote and the link-less FDA post are kept too
    fda = items[-1]
    assert fda.url == "https://x.com/FDAOncology/status/1830000000000000007"
    assert "Links:" not in fda.abstract and fda.doi is None


def test_item_shape_expanded_urls_and_doi():
    items = parse_tweets(_payload(), "kol_x_list", CFG)
    first, doi_post, preprint = items
    assert first.url == "https://x.com/montypal/status/1830000000000000001"
    assert first.title.startswith("@montypal: Phase 3 readout: overall survival benefit")
    assert len(first.title) <= len("@montypal: ") + 120
    assert "https://www.nature.com/articles/s41591-026-01234-5" in first.abstract
    assert "t.co" not in first.abstract
    assert first.abstract.endswith("\nLinks: https://www.nature.com/articles/s41591-026-01234-5")
    assert first.published_at == datetime(2026, 9, 8, 14, 2, 11, tzinfo=UTC)
    raw = json.loads(first.raw_json)
    assert raw["author"] == {"id": "20001", "username": "montypal", "name": "Sumanta (Monty) Pal"}
    assert raw["metrics"]["like_count"] == 88
    assert raw["links"] == ["https://www.nature.com/articles/s41591-026-01234-5"]
    assert raw["referenced_tweets"] == []
    assert doi_post.doi == "10.1200/jco.2026.44.16_suppl.7524"
    assert preprint.doi == "10.64898/2026.09.01.700001v1"


def test_malformed_records_skipped_with_debug(caplog):
    payload = {"data": [{"id": "1"}, "junk", {"text": "no id"}], "includes": {"users": []}}
    with caplog.at_level(logging.DEBUG, logger="ingest.x_list"):
        items, stats = parse_tweets_stats(payload, "k", CFG)
    assert items == [] and stats["malformed"] == 3
    assert "malformed" in caplog.text


def test_fetch_sends_bearer_header_and_start_time_and_stops_at_max_pages(monkeypatch, caplog):
    monkeypatch.setenv("X_BEARER_TOKEN", "sekrit-token")
    monkeypatch.setenv("X_KOL_LIST_ID", "1234567890")
    calls = []

    def fake_get_json(url, *, params=None, user_agent=None, headers=None, **kw):
        calls.append({"url": url, "params": params, "headers": headers})
        return _payload()  # always carries meta.next_token

    from ingest import http

    monkeypatch.setattr(http, "get_json", fake_get_json)
    src = XListSource(dict(CFG, max_pages=2), GLOBAL)
    with caplog.at_level(logging.INFO, logger="ingest.x_list"):
        items = src.fetch()
    assert len(items) == 6  # two identical pages; the DB dedups later
    assert len(calls) == 2
    assert calls[0]["url"] == "https://api.x.com/2/lists/1234567890/tweets"
    assert calls[0]["headers"] == {"Authorization": "Bearer sekrit-token"}
    p = calls[0]["params"]
    assert p["tweet.fields"] == "created_at,public_metrics,entities,referenced_tweets,author_id"
    assert p["expansions"] == "author_id" and p["user.fields"] == "username,name"
    assert p["max_results"] == 100
    assert p["start_time"].endswith("Z")
    datetime.strptime(p["start_time"], "%Y-%m-%dT%H:%M:%SZ")
    assert "pagination_token" not in p
    assert calls[1]["params"]["pagination_token"] == _payload()["meta"]["next_token"]
    assert "sekrit-token" not in caplog.text
    assert "estimated 720 list requests/month" in caplog.text


def test_missing_token_raises_before_any_fetch(monkeypatch):
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("X_KOL_LIST_ID", "1234567890")

    def boom(self, params, token):
        pytest.fail("fetch_page must not be called without a token")

    monkeypatch.setattr(XListSource, "fetch_page", boom)
    with pytest.raises(RuntimeError, match="X_BEARER_TOKEN"):
        XListSource(CFG, GLOBAL).fetch()


def test_missing_list_id_raises_before_any_fetch(monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "t")
    monkeypatch.delenv("X_KOL_LIST_ID", raising=False)

    def boom(self, params, token):
        pytest.fail("fetch_page must not be called without a list id")

    monkeypatch.setattr(XListSource, "fetch_page", boom)
    with pytest.raises(RuntimeError, match="X_KOL_LIST_ID"):
        XListSource(dict(CFG, list_id=""), GLOBAL).fetch()


def test_estimated_requests_per_month():
    assert estimated_requests_per_month({"cadence_minutes": 120, "max_pages": 1}) == 360
    assert estimated_requests_per_month({"cadence_minutes": 60, "max_pages": 3}) == 2160


def test_shipped_config_stays_under_monthly_cap(monkeypatch):
    monkeypatch.delenv("X_KOL_LIST_ID", raising=False)
    kol = load_config()["kol"]
    assert estimated_requests_per_month(kol) <= int(kol["monthly_request_cap"])
    assert int(kol["lookback_hours"]) * 60 > int(kol["cadence_minutes"])
