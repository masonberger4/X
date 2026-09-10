"""ClinicalTrials.gov API v2, filtered by lastUpdatePostDate. Config:

- name: clinicaltrials_oncology
  type: clinicaltrials
  url: https://clinicaltrials.gov/api/v2/studies
  lookback_days: 2
  page_size: 100
  max_pages: 3
  query_cond: cancer OR neoplasm
  query_intr: ""                  # optional intervention query
  phases: [PHASE2, PHASE3]        # optional
  statuses: [RECRUITING, COMPLETED]
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from ingest import http
from ingest.base import Item, Source, utcnow

log = logging.getLogger(__name__)
FIELDS = ",".join(
    [
        "NCTId",
        "BriefTitle",
        "OfficialTitle",
        "BriefSummary",
        "Phase",
        "OverallStatus",
        "LastUpdatePostDate",
        "StartDate",
        "PrimaryCompletionDate",
        "Condition",
        "InterventionName",
        "LeadSponsorName",
        "WhyStopped",
        "ResultsFirstPostDate",
    ]
)


def _date(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def parse_studies(studies: list[dict[str, Any]], source_name: str) -> list[Item]:
    items = []
    for st in studies:
        ps = st.get("protocolSection", {})
        ident = ps.get("identificationModule", {})
        nct = ident.get("nctId")
        title = ident.get("officialTitle") or ident.get("briefTitle")
        if not nct or not title:
            continue
        status = ps.get("statusModule", {})
        design = ps.get("designModule", {})
        arms = ps.get("armsInterventionsModule", {})
        conds = ps.get("conditionsModule", {}).get("conditions", [])
        interventions = [i.get("name") for i in arms.get("interventions", []) if i.get("name")]
        phases = design.get("phases", [])
        sponsor = ps.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {}).get("name")
        summary = ps.get("descriptionModule", {}).get("briefSummary", "")
        meta = (
            f"Status: {status.get('overallStatus')}. Phase: {'/'.join(phases) or 'NA'}. "
            f"Sponsor: {sponsor}. Conditions: {', '.join(conds[:5])}. "
            f"Interventions: {', '.join(interventions[:5])}."
        )
        if status.get("whyStopped"):
            meta += f" Why stopped: {status['whyStopped']}."
        abstract = f"{meta} {summary}".strip()
        published = _date((status.get("lastUpdatePostDateStruct") or {}).get("date"))
        raw = {
            "nctId": nct,
            "status": status.get("overallStatus"),
            "phases": phases,
            "sponsor": sponsor,
            "conditions": conds,
            "interventions": interventions,
            "lastUpdatePostDate": (status.get("lastUpdatePostDateStruct") or {}).get("date"),
            "resultsFirstPostDate": (status.get("resultsFirstPostDateStruct") or {}).get("date"),
        }
        items.append(
            Item.build(
                source=source_name,
                url=f"https://clinicaltrials.gov/study/{nct}",
                title=f"{ident.get('briefTitle') or title} ({nct})",
                abstract=abstract,
                published_at=published,
                raw=raw,
            )
        )
    return items


class ClinicalTrialsSource(Source):
    type = "clinicaltrials"

    def fetch_page(self, params: dict[str, Any]) -> dict[str, Any]:
        return http.get_json(
            self.cfg["url"],
            params=params,
            user_agent=self.user_agent(),
        )

    def build_params(self, page_token: str | None = None) -> dict[str, Any]:
        start = (utcnow() - timedelta(days=int(self.cfg.get("lookback_days", 2)))).date()
        params: dict[str, Any] = {
            "format": "json",
            "pageSize": int(self.cfg.get("page_size", 100)),
            "fields": FIELDS,
            "filter.advanced": f"AREA[LastUpdatePostDate]RANGE[{start.isoformat()},MAX]",
            "sort": "LastUpdatePostDate:desc",
        }
        if self.cfg.get("query_cond"):
            params["query.cond"] = self.cfg["query_cond"]
        if self.cfg.get("query_intr"):
            params["query.intr"] = self.cfg["query_intr"]
        if self.cfg.get("statuses"):
            params["filter.overallStatus"] = ",".join(self.cfg["statuses"])
        if self.cfg.get("phases"):
            params["filter.advanced"] += (
                " AND (" + " OR ".join(f"AREA[Phase]{p}" for p in self.cfg["phases"]) + ")"
            )
        if page_token:
            params["pageToken"] = page_token
        return params

    def fetch(self) -> list[Item]:
        items: list[Item] = []
        token = None
        for _ in range(int(self.cfg.get("max_pages", 3))):
            data = self.fetch_page(self.build_params(token))
            items.extend(parse_studies(data.get("studies", []), self.name))
            token = data.get("nextPageToken")
            if not token:
                break
        log.info("%s: parsed %d studies", self.name, len(items))
        return items
