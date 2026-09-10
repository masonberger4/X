"""config.load_config expansion of `conferences` and `kol` (step 6)."""

import textwrap
from datetime import date

import pytest

from config import load_config

EXPECTED_WINDOWS = {
    "asco": [(date(2027, 5, 27), date(2027, 6, 11))],
    "aacr": [(date(2027, 3, 19), date(2027, 4, 10))],
    "esmo": [(date(2026, 10, 16), date(2026, 10, 30))],
    "ash": [(date(2026, 11, 4), date(2026, 12, 18))],
    "asgct": [(date(2027, 4, 26), date(2027, 5, 11))],
}


@pytest.fixture
def shipped(monkeypatch):
    monkeypatch.delenv("X_KOL_LIST_ID", raising=False)
    return load_config()


def test_shipped_conference_sources(shipped):
    by_name = {s["name"]: s for s in shipped["sources"]}
    meetings = shipped["conferences"]["meetings"]
    assert {m["key"] for m in meetings} == set(EXPECTED_WINDOWS)
    for m in meetings:
        src = by_name[f"conf_{m['key']}_abstracts"]
        assert src["type"] == "crossref"
        assert src["issn"] == m["issn"] and src["issue_pattern"]
        assert src["meeting"] == m["name"] and src["journal"] == m["journal"]
        assert [(w["start"], w["end"]) for w in src["windows"]] == EXPECTED_WINDOWS[m["key"]]
        assert all(w["cadence_minutes"] == 60 for w in src["windows"])
        assert src["cadence_minutes"] == 1440
        assert src["boost_patterns"] == ["LBA", "late-breaking", "plenary"]
        assert src["max_items_per_run"] == 40 and src["rows"] == 200 and src["max_pages"] == 5
        assert "keywords" not in src  # defaults to prefilter.allow_keywords in the source
    assert by_name["conf_esmo_abstracts"]["enabled"] is False
    assert by_name["conf_asgct_abstracts"]["enabled"] is False
    assert "enabled" not in by_name["conf_asco_abstracts"]
    news = by_name["conf_aacr_news"]
    assert news["type"] == "rss" and news["url"].startswith("https://www.aacr.org/")
    assert news["windows"] == by_name["conf_aacr_abstracts"]["windows"]
    assert news["cadence_minutes"] == 360


def test_shipped_kol_source_is_disabled(shipped):
    kol = [s for s in shipped["sources"] if s["type"] == "x_list"]
    assert len(kol) == 1
    src = kol[0]
    assert src["name"] == "kol_x_list" and src["enabled"] is False
    assert src["api_url"] == "https://api.x.com/2" and src["list_id"] == ""
    assert src["require_link"] and src["skip_retweets"] and src["skip_replies"]
    assert src["enforce_handles"] is False
    assert len(src["handles"]) >= 45
    assert all({"handle", "name", "focus"} <= set(h) for h in src["handles"])


def test_shipped_names_unique_and_rss_urls_present(shipped):
    names = [s["name"] for s in shipped["sources"]]
    assert len(names) == len(set(names))
    for s in shipped["sources"]:
        if s["type"] == "rss":
            assert s.get("url"), s["name"]


def test_crossref_block_present(shipped):
    cr = shipped["crossref"]
    assert cr["api_url"] == "https://api.crossref.org/works"
    assert cr["mailto"] and cr["rows"] == 200 and cr["max_pages"] == 5


YAML = """
sources: []
crossref: {api_url: https://api.crossref.org/works, mailto: t@example.org, rows: 50, max_pages: 2}
conferences:
  default_cadence_minutes: 720
  window_cadence_minutes: 30
  max_items_per_run: 5
  boost_patterns: [LBA]
  keywords: [cancer]
  meetings:
    - key: demo
      name: "Demo Meeting"
      journal: "Demo Journal"
      issn: 1234-5678
      issue_pattern: suppl
      windows: [{start: 2026-11-01, end: 2026-11-05, cadence_minutes: 15}]
      news_rss: https://demo.example.org/feed
      news_enabled: false
      rows: 10
    - key: nowin
      name: "No Window"
      issn: 2222-3333
companies:
  feeds:
    - {key: quiet, name: Quiet, url: https://quiet.example.org/rss, enabled: false}
kol:
  enabled: true
  list_id: "99"
  api_url: https://api.x.com/2
  cadence_minutes: 120
  max_pages: 1
  handles: [{handle: a, name: A, focus: x}]
"""


def test_temp_yaml_expansion(tmp_path, monkeypatch):
    monkeypatch.delenv("X_KOL_LIST_ID", raising=False)
    p = tmp_path / "c.yaml"
    p.write_text(textwrap.dedent(YAML))
    cfg = load_config(p)
    by_name = {s["name"]: s for s in cfg["sources"]}
    demo = by_name["conf_demo_abstracts"]
    assert demo["cadence_minutes"] == 720 and demo["rows"] == 10 and demo["max_pages"] == 2
    assert demo["windows"] == [
        {"start": date(2026, 11, 1), "end": date(2026, 11, 5), "cadence_minutes": 15}
    ]
    assert demo["keywords"] == ["cancer"] and demo["max_items_per_run"] == 5
    news = by_name["conf_demo_news"]
    assert news["url"] == "https://demo.example.org/feed" and news["enabled"] is False
    assert news["windows"][0]["cadence_minutes"] == 15
    assert by_name["conf_nowin_abstracts"]["windows"] == []
    assert "conf_nowin_news" not in by_name
    assert by_name["company_quiet"]["enabled"] is False
    kol = by_name["kol_x_list"]
    assert kol["enabled"] is True and kol["list_id"] == "99" and kol["type"] == "x_list"


def test_duplicate_expanded_name_is_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        textwrap.dedent(
            """
            sources:
              - {name: conf_demo_abstracts, type: rss, url: https://x.example.org/rss}
            conferences:
              meetings:
                - {key: demo, name: Demo, issn: 1234-5678}
            """
        )
    )
    with pytest.raises(ValueError, match="conf_demo_abstracts"):
        load_config(p)


def test_no_kol_block_means_no_kol_source(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("sources: []\n")
    assert load_config(p)["sources"] == []
