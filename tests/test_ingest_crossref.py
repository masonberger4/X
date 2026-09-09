"""CrossrefSource (step 6): real JCO/ASCO supplement pages saved from api.crossref.org."""

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

from ingest.crossref import CrossrefSource, meeting_line, parse_works, parse_works_stats

FIX = Path(__file__).parent / "fixtures"
GLOBAL = {
    "http": {"user_agent": "test"},
    "crossref": {"api_url": "https://api.crossref.org/works", "mailto": "t@example.org"},
    "prefilter": {"allow_keywords": ["cancer", "lymphoma", "myeloma", "carcinoma", "tumor"]},
}
CFG = {
    "name": "conf_asco_abstracts",
    "type": "crossref",
    "meeting": "ASCO Annual Meeting",
    "journal": "Journal of Clinical Oncology",
    "issn": "0732-183X",
    "issue_pattern": "suppl",
    "boost_patterns": ["LBA", "late-breaking", "plenary"],
    "lookback_days": 3,
    "rows": 10,
    "max_pages": 3,
    "max_items_per_run": 40,
}


def _works(name):
    return json.loads((FIX / name).read_text())["message"]["items"]


def test_parse_real_supplement_page():
    works = _works("crossref_jco_asco_page.json")
    items = parse_works(works, "conf_asco_abstracts", CFG, GLOBAL)
    assert len(items) == 10
    by_doi = {i.doi: i for i in items}
    it = by_doi["10.1200/jco.2026.44.16_suppl.7087"]
    assert it.url == "https://doi.org/10.1200/jco.2026.44.16_suppl.7087"
    assert it.title.startswith("Evaluating the incidence and predictors of chronic opioid use")
    assert it.published_at == datetime(2026, 5, 28, 0, 0, 0, tzinfo=UTC)
    # JATS stripped, meeting line prepended, title untouched
    assert "<jats:" not in it.abstract and "jats:p" not in it.abstract
    assert it.abstract.startswith(
        "ASCO Annual Meeting 2026 abstract (Journal of Clinical Oncology 44, 16_suppl). "
    )
    assert "Background: Exposure to opioids during cancer treatment" in it.abstract
    assert "ASCO" not in it.title
    raw = json.loads(it.raw_json)
    assert raw == {
        "container-title": "Journal of Clinical Oncology",
        "volume": "44",
        "issue": "16_suppl",
        "type": "journal-article",
        "meeting": "ASCO Annual Meeting",
        "session_hint": None,
    }


def test_issue_pattern_drops_regular_issues():
    works = _works("crossref_jco_lba_page.json")
    assert len(works) == 20
    items, stats = parse_works_stats(works, "c", CFG, GLOBAL)
    assert stats["fetched"] == 20 and stats["issue"] == 10
    assert all(json.loads(i.raw_json)["issue"] == "17_suppl" for i in items)
    assert all("lba" in i.doi for i in items)
    assert json.loads(items[0].raw_json)["session_hint"] == "late-breaking"


def test_issue_pattern_may_match_doi_when_issue_missing():
    w = copy.deepcopy(_works("crossref_jco_asco_page.json")[0])
    w.pop("issue")
    items = parse_works([w], "c", dict(CFG, issue_pattern="16_suppl"), GLOBAL)
    assert len(items) == 1
    assert items[0].abstract.startswith(
        "ASCO Annual Meeting 2026 abstract (Journal of Clinical Oncology 44). "
    )


def test_keyword_gate_drops_non_oncology_title():
    works = copy.deepcopy(_works("crossref_jco_asco_page.json")[:2])
    works[1]["title"] = ["Statin adherence and cardiovascular outcomes in older adults"]
    works[1]["abstract"] = "<jats:p>Nothing about neoplasms here.</jats:p>"
    items, stats = parse_works_stats(works, "c", CFG, GLOBAL)
    assert stats == {"fetched": 2, "issue": 2, "keywords": 1, "kept": 1}
    assert items[0].doi == "10.1200/jco.2026.44.16_suppl.7087"
    # source-level keywords override the prefilter default
    items = parse_works(works, "c", dict(CFG, keywords=["statin"]), GLOBAL)
    assert [i.doi for i in items] == ["10.1200/jco.2026.44.16_suppl.7089"]


def test_boost_ordering_puts_lba_first_then_newest():
    page = copy.deepcopy(_works("crossref_jco_asco_page.json"))
    lbas = _works("crossref_jco_lba_page.json")
    page[3]["created"]["date-time"] = "2026-06-09T00:00:00Z"  # newer than every LBA
    newest = page[3]["DOI"]
    items = parse_works(page + lbas, "c", CFG, GLOBAL)
    n_boosted = sum("lba" in i.doi for i in items)
    assert n_boosted >= 1 and all("lba" in i.doi for i in items[:n_boosted])
    assert items[n_boosted].doi == newest  # first non-boosted item is the newest one
    plain = parse_works(page + lbas, "c", dict(CFG, boost_patterns=[]), GLOBAL)
    assert plain[0].doi == newest


def test_max_items_per_run_truncates():
    works = _works("crossref_jco_asco_page.json")
    items = parse_works(works, "c", dict(CFG, max_items_per_run=3), GLOBAL)
    assert len(items) == 3
    assert len(parse_works(works, "c", dict(CFG, max_items_per_run=0), GLOBAL)) == 10


def test_malformed_records_are_skipped():
    works = ["junk", {}, {"DOI": "10.1/x"}, {"title": ["no doi"]}]
    assert parse_works(works, "c", CFG, GLOBAL) == []


def test_meeting_line_falls_back_to_created_year():
    w = {"created": {"date-time": "2027-04-02T10:00:00Z"}, "issue": "7_Supplement"}
    assert meeting_line(w, {"meeting": "AACR Annual Meeting", "journal": "Cancer Research"}) == (
        "AACR Annual Meeting 2027 abstract (Cancer Research, 7_Supplement)."
    )


def test_fetch_paginates_and_stops_at_max_pages(monkeypatch):
    page = json.loads((FIX / "crossref_jco_asco_page.json").read_text())
    calls = []

    def fake_fetch_page(self, params):
        calls.append(params)
        p = copy.deepcopy(page)
        p["message"]["next-cursor"] = f"cursor-{len(calls)}"
        return p

    monkeypatch.setattr(CrossrefSource, "fetch_page", fake_fetch_page)
    monkeypatch.delenv("CROSSREF_MAILTO", raising=False)
    src = CrossrefSource(CFG, GLOBAL)
    items = src.fetch()
    assert len(calls) == 3
    assert calls[0]["cursor"] == "*"
    assert [c["cursor"] for c in calls[1:]] == ["cursor-1", "cursor-2"]
    for c in calls:
        assert c["mailto"] == "t@example.org"
        assert c["filter"].startswith("issn:0732-183X,from-created-date:")
        assert c["rows"] == 10 and "abstract" in c["select"]
    # Three identical pages: every record is parsed; the DB dedup_hash drops repeats later.
    assert len(items) == 30


def test_fetch_stops_when_no_cursor_and_env_mailto_wins(monkeypatch):
    page = json.loads((FIX / "crossref_jco_asco_page.json").read_text())
    calls = []

    def fake_fetch_page(self, params):
        calls.append(params)
        p = copy.deepcopy(page)
        p["message"].pop("next-cursor", None)
        return p

    monkeypatch.setattr(CrossrefSource, "fetch_page", fake_fetch_page)
    monkeypatch.setenv("CROSSREF_MAILTO", "env@example.org")
    src = CrossrefSource(CFG, GLOBAL)
    assert len(src.fetch()) == 10
    assert len(calls) == 1 and calls[0]["mailto"] == "env@example.org"


def test_fetch_page_uses_http_get_json(monkeypatch):
    from ingest import http

    seen = {}

    def fake_get_json(url, *, params=None, user_agent=None, **kw):
        seen.update(url=url, params=params, user_agent=user_agent)
        return {"message": {"items": []}}

    monkeypatch.setattr(http, "get_json", fake_get_json)
    src = CrossrefSource(CFG, GLOBAL)
    assert src.fetch() == []
    assert seen["url"] == "https://api.crossref.org/works"
    assert seen["user_agent"] == "test"
    assert seen["params"]["cursor"] == "*"
