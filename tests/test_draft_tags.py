"""Hard rule 11: @handles for accounts we know, #hashtags for drug names and NCT numbers."""

from draft import drafter
from draft.prompt import HARD_RULES, build_user_prompt
from draft.schema import validate_output
from draft.tags import (
    company_names,
    drug_names,
    handles_block,
    load_handles,
    nct_ids,
    relevant_handles,
    tag_problems,
)
from swarm.cells import cell_problems
from swarm.genome import HOOK, Slot
from swarm.prompts import Brief, cell_rules, propose_prompt

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


def test_hard_rules_and_prompt_carry_the_rule():
    assert "11. Mentions and hashtags" in HARD_RULES and "@JCO_ASCO" in HARD_RULES
    hs = load_handles(CFG)[:1]
    user = build_user_prompt(title="t", abstract="a", url=URL, source="s", handles=hs)
    assert handles_block(hs) in user and "- @Merck = Merck" in user
    assert "X HANDLES" not in build_user_prompt(title="t", abstract="a", url=URL, source="s")


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


def test_check_hard_rules_enforces_tags():
    hs = load_handles(CFG)[:1]
    d = _draft("Merck data", "KEYNOTE-189 (NCT04487080) read", "third", "last")
    probs = drafter.check_hard_rules(d, url=URL, source="s", handles=hs)
    assert "thread[0] names Merck without its handle @Merck" in probs
    assert "thread[1] trial number(s) without a hashtag: NCT04487080 -> #NCT04487080" in probs
    d = _draft("@Merck data", "KEYNOTE-189 (#NCT04487080) read", "third", "last")
    assert drafter.check_hard_rules(d, url=URL, source="s", handles=hs) == []


def test_numbers_in_ignores_tags():
    assert drafter.numbers_in("#NCT04487080 and @JCO_ASCO: ORR 88% in 40") == ["88%", "40"]


def test_story_handles_reads_root_config(monkeypatch):
    monkeypatch.setattr(drafter, "_root_config", lambda: CFG)
    hs = drafter.story_handles(source_text="Merck says", url=URL, source="pubmed")
    assert [h.handle for h in hs] == ["Merck", "JCO_ASCO"]
    monkeypatch.setattr(drafter, "_root_config", dict)
    assert drafter.story_handles(source_text="Merck says", url=URL, source="pubmed") == []


def test_swarm_cells_and_prompts():
    hs = load_handles(CFG)[:1]
    kw = dict(source_text="Merck 88%", slot=HOOK, is_preprint=False)
    assert cell_problems("Merck reports 88% ORR", handles=hs, **kw) == [
        "names Merck without its handle @Merck"
    ]
    assert cell_problems("@Merck reports 88% ORR", handles=hs, **kw) == []
    assert "#NCT04487080" in cell_rules() and "never invent" in cell_rules()
    brief = Brief(title="t", abstract="a", url=URL, source="s", handles=tuple(hs))
    assert "- @Merck = Merck" in propose_prompt(brief, Slot("hook", "r"), {})
