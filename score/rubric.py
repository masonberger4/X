"""Scoring rubric: system prompt, few-shot examples, and the tool JSON schema.

Bump PROMPT_VERSION whenever the prompt, examples, or schema change so that
old score rows can be told apart from new ones.
"""

from __future__ import annotations

import json
from typing import Any

PROMPT_VERSION = "v1"
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
            "description": "The interpretive angle a post could take (what it means, what to watch, what is overhyped).",
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
            "source": "biorxiv_cancer_biology",
            "title": "A novel small molecule inhibits growth of a colorectal cancer cell line in vitro",
            "abstract": "We report compound X, which reduced viability of HCT116 cells with an IC50 of 3 uM. No in vivo data are presented.",
        },
        "score": {
            "novelty": 3,
            "clinical_significance": 1,
            "audience_interest": 2,
            "expertise_fit": 2,
            "timeliness": 3,
            "evidence_level": "preprint",
            "hype_risk": 4,
            "rationale": "Single cell line, no in vivo data, unreviewed preprint.",
            "suggested_angle": "Not worth a post unless it fits a broader 'why most in-vitro hits die' thread.",
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
            "rationale": "In vivo CAR-T is a hot, under-covered modality but data are topline-only, small n, company-reported.",
            "suggested_angle": "Explain why in vivo CAR-T matters (no apheresis/manufacturing), then flag what the topline release leaves out: durability, DoR, comparator.",
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
    return f"""You are an editor for an X (Twitter) account that covers cancer research for an informed audience: oncologists, biotech investors, patient advocates, and science-literate readers. The account's value is interpretation, not description, and it never gives medical advice.

Score each item on the rubric below. Use the full 0-10 range; most routine items should land in the 2-5 band.

Dimensions (0-10 each):
- novelty: how new is the finding or mechanism relative to what the field already knows? First-in-class, new modality, or a surprising result scores high; incremental confirmations score low.
- clinical_significance: would this change practice or patient outcomes if it holds? Randomized phase 3 data, approvals, and label changes score high; single cell-line studies score near zero.
- audience_interest: would the target audience stop scrolling? Approvals, big trial readouts, controversies, and reversals score high; conference logistics, investor-meeting notices, and routine hiring news score 0-1.
- expertise_fit: how well does this fit the account's specialty? Apply a bonus (+2 to +4, capped at 10) when the item is primarily about any of:
{topics_txt}
  Items about other oncology topics score on plain relevance; non-oncology items score 0-1.
- timeliness: is this news now? Approvals, embargo-lift data, and trial stops score high; reviews, editorials, and re-analyses of old data score low.

Also report:
- evidence_level: one of {", ".join(EVIDENCE_LEVELS)}. Use 'preprint' for bioRxiv/medRxiv, 'approval' for regulatory actions, 'other' for reviews, guidelines, policy, or business news.
- hype_risk (0-10): how likely is the headline to overstate the evidence? Company-reported topline numbers with no comparator, tiny n, surrogate endpoints, or animal data described in clinical language raise this.
- rationale: one line, <= 200 characters, specific to the item.
- suggested_angle: the interpretation a post could offer (what it means, what to watch, what is overhyped). Never suggest treatment recommendations.

Do not invent numbers that are not in the item text. Score items independently of each other. You MUST respond by calling the `{TOOL_NAME}` tool exactly once with one entry per item, using the item's given index.

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
