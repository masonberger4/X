"""Hard rule 11: @handles for accounts we know, #hashtags for drug names and NCT numbers."""

from draft.schema import validate_output
from draft.tags import (
    company_names,
    drug_names,
    load_handles,
    nct_ids,
    relevant_handles,
    tag_problems,
)

CFG = {
    "companies": {
        "feeds": [
            {"key": "merck", "name": "Merck", "url": "https://www.merck.com/feed/", "x": "Merck"},
            {"key": "nkarta", "name": "Nkarta", "url": "https://ir.nkartatx.com/rss"},
        ]
    },
    "branding": {
        "companies": [
            {
                "key": "jnj",
                "name": "Johnson & Johnson",
                "x": "@JNJNews",
                "aliases": ["J&J"],
                "domain": "jnj.com",
            }
        ]
    },
    "mentions": [
        {
            "name": "Journal of Clinical Oncology",
            "handle": "JCO_ASCO",
            "aliases": ["JCO"],
            "domains": ["ascopubs.org"],
        },
        {
            "name": "Blood",
            "handle": "BloodJournal",
            "domains": ["ashpublications.org"],
            "match_names": False,
        },
    ],
}
URL = "https://ascopubs.org/doi/10.1200/JCO.23.01234"


def test_load_handles_from_config():
    hs = load_handles(CFG)
    assert [h.handle for h in hs] == ["Merck", "JNJNews", "JCO_ASCO", "BloodJournal"]
    jnj = hs[1]
    assert jnj.aliases == ("J&J",) and jnj.domains == ("jnj.com",)
    assert hs[0].domains == ("www.merck.com",)
    assert load_handles(None) == [] and load_handles({}) == []


def test_company_names_from_config():
    assert company_names(CFG) == {"merck", "nkarta", "johnson & johnson", "j&j"}
    assert company_names(None) == frozenset()


def test_relevant_handles_by_name_host_and_source():
    hs = load_handles(CFG)
    rel = relevant_handles(hs, source_text="Merck reports data", url=URL, source="pubmed")
    assert [h.handle for h in rel] == ["Merck", "JCO_ASCO"]
    rel = relevant_handles(
        hs, source_text="a release", url="https://www.jnj.com/x", source="company_jnj"
    )
    assert [h.handle for h in rel] == ["JNJNews"]
    # a lower-case common word is not the company; Blood only through its host
    assert relevant_handles(hs, source_text="peripheral blood merck", url="") == []
    rel = relevant_handles(hs, source_text="", url="https://ashpublications.org/blood/1")
    assert [h.handle for h in rel] == ["BloodJournal"]


def test_nct_and_drug_names():
    assert nct_ids("NCT04487080 and nct03215706, again NCT04487080.") == [
        "NCT04487080",
        "NCT03215706",
    ]
    assert nct_ids("#NCT04487080 is tagged; KEYNOTE-189 and NCT123 are not numbers") == []
    assert nct_ids("see https://clinicaltrials.gov/study/NCT04487080") == []
    assert drug_names("trastuzumab deruxtecan, cilta-cel and osimertinib") == [
        "trastuzumab",
        "cilta-cel",
        "osimertinib",
    ]
    assert drug_names("#Trastuzumab Deruxtecan (T-DXd) and #cilta-cel; cancel the cab") == []
    # companies named like antibodies are never drugs: built in, or from config
    assert drug_names("Genmab and Alphamab sell epcoritamab") == ["epcoritamab"]
    assert drug_names("Examplemab sells epcoritamab", {"examplemab"}) == ["epcoritamab"]
    assert tag_problems("Examplemab's drug", None, {"examplemab"}) == []


def test_tag_problems():
    hs = load_handles(CFG)[:1]
    assert tag_problems("Merck's engager in KEYNOTE-189 with #pembrolizumab", hs) == [
        "names Merck without its handle @Merck"
    ]
    assert tag_problems("@Merck's engager in KEYNOTE-189 with #pembrolizumab", hs) == []
    probs = tag_problems("KEYNOTE-189 (NCT04487080): pembrolizumab wins", None)
    assert probs == [
        "trial number(s) without a hashtag: NCT04487080 -> #NCT04487080",
        "drug name(s) without a hashtag: pembrolizumab -> #pembrolizumab",
    ]


def _draft(*posts):
    return validate_output(
        {
            "thread": list(posts),
            "why_it_matters": "w",
            "claims_to_verify": [],
            "suggested_visual": "v",
            "chart": {"title": "t", "labels": ["a", "b"], "values": [1, 2], "unit": ""},
            "table": None,
        }
    )
