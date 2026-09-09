"""End to end through run_ingest.ingest (step 6): a Crossref abstract, an RSS item and a
KOL post about the same DOI land in one cluster; window cadence drives re-fetching."""

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import db as db_mod
import run_ingest
from db import Database
from filter.prefilter import cluster_text
from ingest import http
from run_ingest import ingest

FIX = Path(__file__).parent / "fixtures"
DOI = "10.1200/jco.2026.44.16_suppl.7524"
RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>JCO</title><link>https://ascopubs.org</link>
<item>
  <title>Talquetamab plus teclistamab dose selection in extramedullary myeloma</title>
  <link>https://ascopubs.org/doi/10.1200/JCO.2026.44.16_suppl.7524</link>
  <description>Table-of-contents entry for the ASCO abstract on Tal + Tec (RP2R).</description>
</item>
</channel></rss>"""


def _cfg(tmp_path, today):
    return {
        "db_path": str(tmp_path / "t.db"),
        "http": {"user_agent": "t"},
        "crossref": {"api_url": "https://api.crossref.test/works", "mailto": "t@example.org"},
        "dedup": {"title_similarity": 0.92, "near_dup_window_days": 14},
        "prefilter": {"allow_keywords": ["cancer", "myeloma", "lymphoma", "tumor"]},
        "sources": [
            {
                "name": "conf_asco_abstracts",
                "type": "crossref",
                "meeting": "ASCO Annual Meeting",
                "journal": "Journal of Clinical Oncology",
                "issn": "0732-183X",
                "issue_pattern": "suppl",
                "boost_patterns": ["LBA"],
                "rows": 10,
                "max_pages": 2,
                "max_items_per_run": 40,
                "cadence_minutes": 1440,
                "windows": [
                    {
                        "start": (today - timedelta(days=1)).isoformat(),
                        "end": (today + timedelta(days=1)).isoformat(),
                        "cadence_minutes": 60,
                    }
                ],
            },
            {"name": "jco_rss", "type": "rss", "url": "https://j/rss", "cadence_minutes": 1440},
            {
                "name": "kol_x_list",
                "type": "x_list",
                "enabled": True,
                "api_url": "https://api.x.test/2",
                "list_id": "42",
                "cadence_minutes": 120,
                "max_pages": 1,
                "lookback_hours": 6,
                "require_link": True,
                "skip_retweets": True,
                "skip_replies": True,
                "enforce_handles": False,
                "handles": [],
            },
        ],
    }


def test_crossref_rss_and_kol_share_one_cluster_and_windows_drive_cadence(tmp_path, monkeypatch):
    t0 = datetime.now(UTC).replace(microsecond=0)
    clock = {"now": t0}
    monkeypatch.setattr(run_ingest, "utcnow", lambda: clock["now"])
    monkeypatch.setattr(db_mod, "utcnow", lambda: clock["now"])
    monkeypatch.setenv("X_BEARER_TOKEN", "tok")
    monkeypatch.delenv("X_KOL_LIST_ID", raising=False)

    crossref_page = json.loads((FIX / "crossref_jco_asco_page.json").read_text())
    crossref_page["message"].pop("next-cursor")
    tweets = json.loads((FIX / "x_list_tweets.json").read_text())
    tweets["meta"].pop("next_token")
    calls = []

    def fake_get_json(url, *, params=None, user_agent=None, headers=None, **kw):
        calls.append(url)
        if url.startswith("https://api.crossref.test/"):
            assert headers is None and params["mailto"] == "t@example.org"
            return copy.deepcopy(crossref_page)
        if url == "https://api.x.test/2/lists/42/tweets":
            assert headers == {"Authorization": "Bearer tok"}
            return copy.deepcopy(tweets)
        raise AssertionError(url)

    monkeypatch.setattr(http, "get_json", fake_get_json)
    monkeypatch.setattr(http, "get_text", lambda url, **kw: RSS)

    cfg = _cfg(tmp_path, t0.date())
    db = Database(cfg["db_path"])
    try:
        s = ingest(db, cfg)
        assert s["conf_asco_abstracts"] == {"fetched": 10, "inserted": 10, "clusters_new": 10}
        assert s["jco_rss"] == {"fetched": 1, "inserted": 1, "clusters_new": 0}
        assert s["kol_x_list"] == {"fetched": 3, "inserted": 3, "clusters_new": 2}

        cl = db.find_cluster_by_doi(DOI)
        assert cl is not None
        items = db.items_in_cluster(cl.id)
        assert sorted(i.source for i in items) == ["conf_asco_abstracts", "jco_rss", "kol_x_list"]
        title, abstract = cluster_text(items)
        assert abstract.startswith(
            "ASCO Annual Meeting 2026 abstract (Journal of Clinical Oncology"
        )
        assert "Tal + Tec" not in abstract[:100] or "Background:" in abstract  # paper, not tweet
        assert not abstract.startswith("@")
        assert db.counts()["clusters"] == 12

        # second run inside the cadence: nothing is due
        clock["now"] = t0 + timedelta(minutes=30)
        n_calls = len(calls)
        assert ingest(db, cfg) == {}
        assert len(calls) == n_calls

        # inside the meeting window the crossref source is due again after 60 min;
        # the rss (1440) and kol (120) sources are not
        clock["now"] = t0 + timedelta(minutes=61)
        s = ingest(db, cfg)
        assert set(s) == {"conf_asco_abstracts"}
        assert s["conf_asco_abstracts"] == {"fetched": 10, "inserted": 0, "clusters_new": 0}

        # outside the window the default cadence applies
        cfg["sources"][0]["windows"] = []
        clock["now"] = t0 + timedelta(minutes=125)
        s = ingest(db, cfg)
        assert set(s) == {"kol_x_list"}

        # fail soft: a missing token is recorded in source_runs.error, run continues
        monkeypatch.delenv("X_BEARER_TOKEN")
        clock["now"] = t0 + timedelta(days=2)
        s = ingest(db, cfg)
        assert s["kol_x_list"]["error"] == 1 and s["jco_rss"]["fetched"] == 1
        err = db.conn.execute("SELECT error FROM source_runs WHERE source='kol_x_list'").fetchone()[
            0
        ]
        assert "X_BEARER_TOKEN" in err
    finally:
        db.close()
