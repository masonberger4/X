"""Scoring rubric: system prompt, few-shot examples, and the tool JSON schema.

Bump PROMPT_VERSION whenever the prompt, examples, or schema change so that
old score rows can be told apart from new ones.
"""

from __future__ import annotations

import json
from typing import Any

PROMPT_VERSION = "v3"
TOOL_NAME = "score_items"

EVIDENCE_LEVELS = ["preclinical", "preprint", "phase1", "phase2", "phase3", "approval", "other"]
DIMENSIONS = [
    "novelty",
    "clinical_significance",
    "audience_interest",
    "expertise_fit",
    "timeliness",
]

_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "index": {"type": "integer", "description": "Index of the item being scored (as given)."},
        "novelty": {"type": "integer", "minimum": 0, "maximum": 10},
        "clinical_significance": {"type": "integer", "minimum": 0, "maximum": 10},
        "audience_interest": {"type": "integer", "minimum": 0, "maximum": 10},
        "expertise_fit": {"type": "integer", "minimum": 0, "maximum": 10},
        "timeliness": {"type": "integer", "minimum": 0, "maximum": 10},
        "evidence_level": {"type": "string", "enum": EVIDENCE_LEVELS},
        "hype_risk": {"type": "integer", "minimum": 0, "maximum": 10},
        "rationale": {"type": "string", "description": "One line, <= 200 chars."},
        "suggested_angle": {
            "type": "string",
            "description": "The interpretive angle a post could take: what the result means for the science and for the company's thesis, what to watch next (readout, PDUFA, competitor data), what is overhyped. Never a buy/sell call.",
        },
    },
    "required": [
        "index",
        *DIMENSIONS,
        "evidence_level",
        "hype_risk",
        "rationale",
        "suggested_angle",
    ],
    "additionalProperties": False,
}

TOOL: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": "Record rubric scores for every item in the batch. Call exactly once with one entry per item.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {"scores": {"type": "array", "items": _ITEM_SCHEMA}},
        "required": ["scores"],
        "additionalProperties": False,
    },
}


def compute_total(s: dict[str, Any]) -> int:
    """Deterministic total: sum of the five 0-10 dimensions minus half the hype risk.
    Range 0-50 (hype can only subtract)."""
    base = sum(int(s[d]) for d in DIMENSIONS)
    return max(0, base - int(s.get("hype_risk", 0)) // 2)


FEW_SHOT: list[dict[str, Any]] = [
    {
        "item": {
            "source": "fda_oce",
            "title": "FDA approves a BCMA-directed CAR T-cell therapy for second-line multiple myeloma",
            "abstract": "On [date] the FDA approved ... for adults with relapsed or refractory multiple myeloma after one prior line of therapy, based on a randomized phase 3 trial showing a significant improvement in progression-free survival versus standard regimens.",
        },
        "score": {
            "novelty": 7,
            "clinical_significance": 9,
            "audience_interest": 9,
            "expertise_fit": 10,
            "timeliness": 10,
            "evidence_level": "approval",
            "hype_risk": 2,
            "rationale": "Label expansion moves CAR-T earlier in myeloma; phase 3 PFS win; cell therapy is core expertise.",
            "suggested_angle": "What earlier-line CAR-T means for sequencing versus bispecifics, and the manufacturing/access bottleneck to watch.",
        },
    },
    {
        "item": {
            "source": "company_pr",
            "title": "Large pharma to acquire a clinical-stage T-cell engager company for $1.4 billion in cash plus a contingent value right",
            "abstract": "The acquisition adds a phase 2 CD3xBCMA T-cell engager and a preclinical trispecific platform. The deal is expected to close in the first quarter, subject to customary conditions. The CVR pays on a regulatory approval milestone.",
        },
        "score": {
            "novelty": 6,
            "clinical_significance": 5,
            "audience_interest": 10,
            "expertise_fit": 10,
            "timeliness": 10,
            "evidence_level": "other",
            "hype_risk": 3,
            "rationale": "M&A in the core beat with a disclosed price and CVR; clinical value rests on phase 2 data the release does not restate.",
            "suggested_angle": "What the price says about how pharma values mid-stage T-cell engagers versus CAR-T, who the comparable public companies are, and what the CVR structure says about the acquirer's confidence in approval.",
        },
    },
    {
        "item": {
            "source": "biorxiv_cancer_biology",
            "title": "A novel small molecule inhibits growth of a colorectal cancer cell line in vitro",
            "abstract": "We report compound X, which reduced viability of HCT116 cells with an IC50 of 3 uM. No in vivo data are presented.",
        },
        "score": {
            "novelty": 3,
            "clinical_significance": 1,
            "audience_interest": 1,
            "expertise_fit": 1,
            "timeliness": 3,
            "evidence_level": "preprint",
            "hype_risk": 4,
            "rationale": "Single cell line, no in vivo data, unreviewed preprint, no company or asset attached.",
            "suggested_angle": "Not worth a post.",
        },
    },
    {
        "item": {
            "source": "company_pr",
            "title": "Company announces positive topline results from phase 2 study of an in vivo CAR-T candidate in B-cell lymphoma",
            "abstract": "Topline data: ORR 65% (n=20), CR 40%, no grade >=3 CRS; full data to be presented at a future medical meeting. The company plans to initiate a registrational trial.",
        },
        "score": {
            "novelty": 8,
            "clinical_significance": 6,
            "audience_interest": 8,
            "expertise_fit": 10,
            "timeliness": 8,
            "evidence_level": "phase2",
            "hype_risk": 7,
            "rationale": "In vivo CAR-T is a hot, under-covered modality but data are topline-only, small n, company-reported; a registrational trial is the next catalyst.",
            "suggested_angle": "Explain why in vivo CAR-T matters commercially (no apheresis or manufacturing slot, a cost structure closer to a biologic), then flag what the topline release leaves out (durability, DoR, comparator) and what the registrational trial design will need to show for the thesis to hold.",
        },
    },
]


def build_system_prompt(expertise_cfg: dict[str, Any]) -> str:
    topics = expertise_cfg.get("bonus_topics") or []
    topics_txt = "\n".join(f"  - {t}" for t in topics) or "  - (none configured)"
    examples = "\n\n".join(
        f"Example input:\n{json.dumps(ex['item'], indent=2)}\nExample output entry:\n"
        f"{json.dumps(ex['score'], indent=2)}"
        for ex in FEW_SHOT
    )
    return f"""You are a PhD-level immuno-oncology analyst at a hedge fund, triaging the day's news for an X (Twitter) account about the business and investing side of immuno-oncology biotech: CAR-T and other engineered cell therapies, T-cell engagers and bispecifics, and adjacent cutting-edge immuno-oncology. The account covers trial results and what they mean, upcoming readouts and catalysts for public companies, and M&A, licensing and financing in the space, with equal weight on the science and the investment implications. Its value is interpretation, not description. It never gives medical advice and never gives investment advice (no buy/sell calls, no price targets).

Score each item on the rubric below. Use the full 0-10 range; most routine items should land in the 2-5 band.

Dimensions (0-10 each):
- novelty: how new is the finding, mechanism, or deal relative to what the field and the market already know? First-in-class, new modality, a surprising result, or an unexpected acquirer scores high; incremental confirmations and expected follow-ups score low.
- clinical_significance: would this change practice or patient outcomes if it holds? Randomized phase 3 data, approvals, and label changes score high; single cell-line studies score near zero. For pure business news (a financing, a licensing deal) score the clinical significance of the underlying asset, not the deal.
- audience_interest: would a biotech investor or analyst stop scrolling? Score high when the item moves or tests a company's thesis: pivotal readouts, approvals or CRLs, M&A and licensing, trial holds or discontinuations, competitor data that reads across, dilutive financings after data. Public-company involvement raises this; a named ticker or acquirer raises it further. Conference logistics, investor-meeting notices, inducement grants, and routine hiring news score 0-1.
- expertise_fit: how well does this fit the account's beat? Apply a bonus (+2 to +4, capped at 10) when the item is primarily about any of:
{topics_txt}
  Items about other oncology topics score on plain relevance; non-oncology items score 0-1.
- timeliness: is this news now? Approvals, embargo-lift data, deal announcements, and trial stops score high; reviews, editorials, and re-analyses of old data score low. An upcoming catalyst with a date (a PDUFA, a scheduled readout) scores high because the post can be written ahead of it.

Also report:
- evidence_level: one of {", ".join(EVIDENCE_LEVELS)}. Use 'preprint' for bioRxiv/medRxiv, 'approval' for regulatory actions, 'other' for reviews, guidelines, policy, or business news.
- hype_risk (0-10): how likely is the headline to overstate the evidence? Company-reported topline numbers with no comparator, tiny n, surrogate endpoints, or animal data described in clinical language raise this.
- rationale: one line, <= 200 characters, specific to the item.
- suggested_angle: the interpretation a post could offer: what the result means for the science AND for the company's thesis (competitive position, what the next catalyst is, what the market may be missing), what to watch, what is overhyped. Name the company and the catalyst where the item gives them. Never suggest treatment recommendations and never suggest buying, selling or shorting anything.

Do not invent numbers that are not in the item text. Do not state a drug's mechanism, target, modality, sponsor, or ticker unless the item text states it or you are certain of it; when unsure, describe what the text says ("the sponsor", "the agent") rather than guess. A wrong mechanism or sponsor in the rationale misleads the editor more than a missing one. Score items independently of each other. You MUST respond by calling the `{TOOL_NAME}` tool exactly once with one entry per item, using the item's given index.

{examples}"""


def build_user_message(batch: list[dict[str, Any]]) -> str:
    lines = ["Score the following items. Respond only via the tool call.", ""]
    for i, it in enumerate(batch):
        lines.append(f"### Item {i}")
        lines.append(f"source: {it['source']}")
        if it.get("published_at"):
            lines.append(f"published_at: {it['published_at']}")
        if it.get("doi"):
            lines.append(f"doi: {it['doi']}")
        lines.append(f"title: {it['title']}")
        lines.append(f"abstract: {it.get('abstract') or '(none)'}")
        lines.append("")
    return "\n".join(lines)
