from datetime import UTC
from pathlib import Path

from ingest import http
from ingest.rss import RSSSource, parse_feed, strip_html

FIX = Path(__file__).parent / "fixtures"


def _src(name, url="https://example.org/feed"):
    return RSSSource(
        {"name": name, "type": "rss", "url": url, "cadence_minutes": 60},
        {"http": {"user_agent": "test-agent"}},
    )


def test_parse_rss20_fda_press():
    items = parse_feed((FIX / "fda_press.xml").read_text(), "fda_press")
    assert len(items) == 20
    it = items[0]
    assert it.source == "fda_press"
    assert it.title.startswith("FDA Takes Steps")
    assert it.url.startswith("http://www.fda.gov/news-events/press-announcements/")
    assert it.published_at is not None and it.published_at.tzinfo == UTC
    assert it.abstract.startswith("The U.S. Food and Drug Administration")
    assert "<" not in it.abstract
    assert it.doi is None
    assert len({i.dedup_hash for i in items}) == 20


def test_parse_rdf_nature_medicine_extracts_doi():
    items = parse_feed((FIX / "nature_medicine.rdf").read_text(), "nature_medicine")
    assert len(items) == 8
    it = items[0]
    assert it.doi == "10.1038/s41591-026-04641-x"
    assert it.url == "https://www.nature.com/articles/s41591-026-04641-x"
    assert it.published_at is not None  # from dc:date / updated
    assert "Nature Medicine, Published online" in it.abstract


def test_parse_company_feed_regeneron():
    items = parse_feed((FIX / "regeneron.xml").read_text(), "company_regeneron")
    assert len(items) == 10
    assert all(i.url.startswith("https://investor.regeneron.com/") for i in items)
    assert "&nbsp;" not in items[0].abstract


def test_unparseable_raises():
    import pytest

    with pytest.raises(ValueError):
        parse_feed("<html><body>Not a feed</body></html>", "x")


def test_fetch_uses_mocked_network(monkeypatch):
    calls = []

    def fake_get_text(url, *, params=None, user_agent=None, timeout=30.0):
        calls.append((url, user_agent))
        return (FIX / "regeneron.xml").read_text()

    monkeypatch.setattr(http, "get_text", fake_get_text)
    src = _src("company_regeneron", "https://example.org/rss")
    items = src.fetch()
    assert calls == [("https://example.org/rss", "test-agent")]
    assert len(items) == 10


def test_strip_html():
    assert strip_html("<p>Hello&nbsp;<b>world</b></p>\n  again") == "Hello world again"
