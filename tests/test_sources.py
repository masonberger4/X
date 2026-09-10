import json
from datetime import UTC, datetime
from pathlib import Path

from Bio import Entrez

from ingest import http
from ingest.biorxiv import BiorxivSource, parse_collection
from ingest.clinicaltrials import ClinicalTrialsSource, parse_studies
from ingest.fda_oce import FDAOCESource, parse_oce_page
from ingest.pubmed import PubMedSource, parse_efetch

FIX = Path(__file__).parent / "fixtures"
GLOBAL = {"http": {"user_agent": "test"}, "ncbi": {"email": "t@example.org", "tool": "t"}}


# ---- bioRxiv ---------------------------------------------------------------
def test_biorxiv_parse_fixture():
    data = json.loads((FIX / "biorxiv_api.json").read_text())
    items = parse_collection(data["collection"], "biorxiv_cancer_biology", "biorxiv")
    assert len(items) == 18
    it = items[0]
    assert it.doi == "10.64898/2026.04.19.716936"
    assert it.url == "https://www.biorxiv.org/content/10.64898/2026.04.19.716936v2"
    assert it.published_at == datetime(2026, 9, 7, tzinfo=UTC)
    assert it.abstract.startswith("Hepatocellular carcinoma")


def test_biorxiv_fetch_paginates(monkeypatch):
    data = json.loads((FIX / "biorxiv_api.json").read_text())
    calls = []

    def fake_get_json(url, *, params=None, user_agent=None, timeout=30.0):
        calls.append((url, params))
        page = dict(data)
        page["messages"] = [dict(data["messages"][0], total="36")]
        return page

    monkeypatch.setattr(http, "get_json", fake_get_json)
    src = BiorxivSource(
        {
            "name": "b",
            "type": "biorxiv",
            "server": "biorxiv",
            "api_url": "https://api.biorxiv.org/details",
            "category": "cancer_biology",
            "lookback_days": 2,
            "max_pages": 5,
        },
        GLOBAL,
    )
    items = src.fetch()
    assert len(calls) == 2
    assert calls[0][0].startswith("https://api.biorxiv.org/details/biorxiv/")
    assert calls[0][0].endswith("/0") and calls[1][0].endswith("/18")
    assert calls[0][1] == {"category": "cancer_biology"}
    assert len(items) == 36


# ---- PubMed ----------------------------------------------------------------
def test_pubmed_parse_efetch_fixture():
    with open(FIX / "pubmed_efetch.xml", "rb") as fh:
        records = Entrez.read(fh)
    items = parse_efetch(records, "pubmed_x")
    assert len(items) == 4
    assert all(i.url.startswith("https://pubmed.ncbi.nlm.nih.gov/") for i in items)
    assert items[0].doi == "10.1016/j.nrleng.2026.502012"
    assert any(i.abstract for i in items)
    assert all(i.published_at is not None for i in items)
    assert json.loads(items[0].raw_json)["pmid"] == "42711043"


def test_pubmed_fetch_mocked(monkeypatch):
    with open(FIX / "pubmed_efetch.xml", "rb") as fh:
        records = Entrez.read(fh)
    ids = json.loads((FIX / "pubmed_esearch.json").read_text())["esearchresult"]["idlist"]
    seen = {}
    monkeypatch.setattr(
        PubMedSource,
        "esearch",
        lambda self, term, mindate, maxdate, retmax: seen.update(term=term, retmax=retmax) or ids,
    )
    monkeypatch.setattr(PubMedSource, "efetch", lambda self, ids_: seen.update(ids=ids_) or records)
    src = PubMedSource(
        {"name": "pubmed_x", "type": "pubmed", "query": "cancer AND CAR-T", "max_results": 50},
        GLOBAL,
    )
    items = src.fetch()
    assert seen["term"] == "cancer AND CAR-T" and seen["retmax"] == 50 and seen["ids"] == ids
    assert len(items) == 4
    assert Entrez.email == "t@example.org"


# ---- ClinicalTrials.gov ----------------------------------------------------
def test_ct_parse_fixture():
    data = json.loads((FIX / "clinicaltrials_page.json").read_text())
    items = parse_studies(data["studies"], "ct")
    assert len(items) == 2
    a, b = items
    assert a.url == "https://clinicaltrials.gov/study/NCT09990001"
    assert "(NCT09990001)" in a.title
    assert a.published_at == datetime(2026, 9, 8, tzinfo=UTC)
    assert "Phase: PHASE2" in a.abstract and "bispecific" in a.abstract
    assert "Why stopped: Lack of efficacy" in b.abstract


def test_ct_fetch_params_and_pagination(monkeypatch):
    data = json.loads((FIX / "clinicaltrials_page.json").read_text())
    calls = []

    def fake_get_json(url, *, params=None, user_agent=None, timeout=30.0):
        calls.append(params)
        if params.get("pageToken"):
            return {"studies": data["studies"][:1]}
        return data

    monkeypatch.setattr(http, "get_json", fake_get_json)
    src = ClinicalTrialsSource(
        {
            "name": "ct",
            "type": "clinicaltrials",
            "url": "https://clinicaltrials.gov/api/v2/studies",
            "query_cond": "cancer",
            "phases": ["PHASE2", "PHASE3"],
            "statuses": ["RECRUITING"],
            "lookback_days": 2,
            "max_pages": 5,
        },
        GLOBAL,
    )
    items = src.fetch()
    assert len(items) == 3
    assert len(calls) == 2 and calls[1]["pageToken"] == "tok2"
    p = calls[0]
    assert p["query.cond"] == "cancer"
    assert p["filter.overallStatus"] == "RECRUITING"
    assert "AREA[LastUpdatePostDate]RANGE[" in p["filter.advanced"]
    assert "AREA[Phase]PHASE3" in p["filter.advanced"]


# ---- FDA OCE ---------------------------------------------------------------
def test_fda_oce_parse_fixture():
    base = "https://www.fda.gov/drugs/resources-information-approved-drugs/oce"
    items = parse_oce_page((FIX / "fda_oce.html").read_text(), "fda_oce", base)
    assert len(items) == 2
    a, b = items
    assert a.url == (
        "https://www.fda.gov/drugs/resources-information-approved-drugs/"
        "fda-approves-examplimab-advanced-urothelial-carcinoma"
    )
    assert a.title.startswith("FDA approved examplimab")
    assert a.published_at == datetime(2026, 9, 3, tzinfo=UTC)
    assert b.published_at == datetime(2026, 8, 28, tzinfo=UTC)
    assert "CAR T-cell" in b.abstract


def test_fda_oce_fails_soft(monkeypatch):
    def boom(url, **kw):
        raise RuntimeError("401 blocked")

    monkeypatch.setattr(http, "get_text", boom)
    src = FDAOCESource({"name": "fda_oce", "type": "fda_oce", "url": "https://x"}, GLOBAL)
    assert src.fetch() == []
    monkeypatch.setattr(
        http, "get_text", lambda url, **kw: "<html><body><p>nothing</p></body></html>"
    )
    assert src.fetch() == []


def test_a_source_user_agent_overrides_the_global_one_and_falls_back():
    from ingest.clinicaltrials import ClinicalTrialsSource

    base = {
        "name": "ct",
        "type": "clinicaltrials",
        "url": "https://clinicaltrials.gov/api/v2/studies",
    }
    assert ClinicalTrialsSource(base, GLOBAL).user_agent() == "test"
    own = ClinicalTrialsSource({**base, "user_agent": "python-httpx/0.28 x/1"}, GLOBAL)
    assert own.user_agent() == "python-httpx/0.28 x/1"
    assert ClinicalTrialsSource(base, {}).user_agent() is None


def test_fetch_page_sends_the_source_user_agent(monkeypatch):
    from ingest import http
    from ingest.clinicaltrials import ClinicalTrialsSource

    seen = {}

    def fake_get_json(url, *, params=None, user_agent=None, timeout=30.0):
        seen["ua"] = user_agent
        return {"studies": []}

    monkeypatch.setattr(http, "get_json", fake_get_json)
    src = ClinicalTrialsSource(
        {"name": "ct", "type": "clinicaltrials", "url": "u", "user_agent": "python-httpx/0.28 p/1"},
        GLOBAL,
    )
    src.fetch_page({})
    assert seen["ua"] == "python-httpx/0.28 p/1"


def test_the_shipped_clinicaltrials_source_names_the_python_client():
    """ClinicalTrials.gov's firewall blocks a Python client that claims to be a browser."""
    import config

    cfg = config.load_config()
    ct = next(s for s in cfg["sources"] if s["type"] == "clinicaltrials")
    assert "python-httpx/" in ct.get("user_agent", "")
