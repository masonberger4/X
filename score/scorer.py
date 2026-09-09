"""Score clusters with the Anthropic API via tool use, in batches, with retry/backoff.

Network is confined to `Scorer.create_message`, which tests replace.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import timezone
from typing import Any

import anthropic

from db import Cluster, Database, Score
from filter.prefilter import cluster_text
from ingest.base import utcnow
from score import rubric

log = logging.getLogger(__name__)

RETRYABLE = (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError)


class ScoringError(RuntimeError):
    pass


class Scorer:
    def __init__(self, cfg: dict[str, Any], client: anthropic.Anthropic | None = None):
        self.cfg = cfg
        self.model: str = cfg["models"]["scorer"]
        sc = cfg.get("scoring") or {}
        self.batch_size = int(sc.get("batch_size", 10))
        self.max_tokens = int(sc.get("max_tokens", 4096))
        self.max_retries = int(sc.get("max_retries", 5))
        self.backoff = float(sc.get("backoff_seconds", 2))
        self.force_tool_choice = bool(sc.get("force_tool_choice", True))
        self.abstract_max_chars = int(sc.get("abstract_max_chars", 2500))
        self.system_prompt = rubric.build_system_prompt(cfg.get("expertise") or {})
        self._client = client

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic()
        return self._client

    # ---- network ----------------------------------------------------------
    def create_message(self, user_content: str) -> Any:
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=self.system_prompt,
            tools=[rubric.TOOL],
            messages=[{"role": "user", "content": user_content}],
        )
        if self.force_tool_choice:
            kwargs["tool_choice"] = {"type": "tool", "name": rubric.TOOL_NAME}
        return self.client.messages.create(**kwargs)

    def create_with_retry(self, user_content: str) -> Any:
        for attempt in range(self.max_retries + 1):
            try:
                return self.create_message(user_content)
            except RETRYABLE as exc:
                if attempt >= self.max_retries:
                    raise
                delay = self.backoff * (2 ** attempt)
                log.warning("API error (%s); retry %d/%d in %.1fs", type(exc).__name__,
                            attempt + 1, self.max_retries, delay)
                time.sleep(delay)
        raise ScoringError("unreachable")

    # ---- parsing ------------------------------------------------------------
    @staticmethod
    def extract_scores(response: Any) -> list[dict[str, Any]]:
        if getattr(response, "stop_reason", None) == "refusal":
            raise ScoringError("model refused the request")
        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == rubric.TOOL_NAME:
                data = block.input if isinstance(block.input, dict) else json.loads(block.input)
                return list(data["scores"])
        raise ScoringError("no tool_use block in response")

    @staticmethod
    def raw_json(response: Any) -> str:
        if hasattr(response, "model_dump_json"):
            return response.model_dump_json()
        return json.dumps(response, default=str)

    # ---- orchestration ----------------------------------------------------
    def batch_payload(self, db: Database, clusters: list[Cluster]) -> list[dict[str, Any]]:
        out = []
        for cl in clusters:
            items = db.items_in_cluster(cl.id)
            title, abstract = cluster_text(items) if items else (cl.title, "")
            sources = sorted({i.source for i in items}) or ["?"]
            doi = cl.doi or next((i.doi for i in items if i.doi), None)
            out.append({
                "cluster_id": cl.id, "source": ", ".join(sources), "title": title,
                "abstract": abstract[: self.abstract_max_chars], "doi": doi,
                "published_at": cl.published_at.isoformat() if cl.published_at else None,
            })
        return out

    def score_batch(self, db: Database, clusters: list[Cluster]) -> list[Score]:
        payload = self.batch_payload(db, clusters)
        response = self.create_with_retry(rubric.build_user_message(payload))
        raw = self.raw_json(response)
        by_index = {int(s["index"]): s for s in self.extract_scores(response)}
        now = utcnow().astimezone(timezone.utc)
        out: list[Score] = []
        for i, p in enumerate(payload):
            s = by_index.get(i)
            if s is None:
                log.warning("cluster %s missing from model output; skipping", p["cluster_id"])
                continue
            score = Score(
                cluster_id=p["cluster_id"], model=self.model,
                prompt_version=rubric.PROMPT_VERSION,
                novelty=int(s["novelty"]), clinical_significance=int(s["clinical_significance"]),
                audience_interest=int(s["audience_interest"]), expertise_fit=int(s["expertise_fit"]),
                timeliness=int(s["timeliness"]), evidence_level=str(s["evidence_level"]),
                hype_risk=int(s["hype_risk"]), total=rubric.compute_total(s),
                rationale=str(s["rationale"])[:300], suggested_angle=str(s["suggested_angle"]),
                raw_response=raw, scored_at=now,
            )
            db.insert_score(score)
            out.append(score)
            log.debug("scored cluster %s total=%d: %s", score.cluster_id, score.total, p["title"][:70])
        return out

    def score_unscored(self, db: Database, limit: int | None = None) -> list[Score]:
        clusters = db.unscored_clusters(self.model, rubric.PROMPT_VERSION)
        if limit is not None:
            clusters = clusters[:limit]
        log.info("scoring %d clusters with %s (%s) in batches of %d",
                 len(clusters), self.model, rubric.PROMPT_VERSION, self.batch_size)
        scores: list[Score] = []
        for i in range(0, len(clusters), self.batch_size):
            batch = clusters[i:i + self.batch_size]
            try:
                scores.extend(self.score_batch(db, batch))
            except (ScoringError, anthropic.APIError) as exc:
                log.error("batch %d-%d failed: %s", i, i + len(batch), exc)
        log.info("scored %d clusters", len(scores))
        return scores
