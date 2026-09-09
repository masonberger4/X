"""Score clusters with the Anthropic API via tool use, in batches, with retry/backoff.

Network is confined to `Scorer.create_message`, which tests replace. With
`models.backend: claude_code` (or LLM_BACKEND=claude_code) the same method runs the
Claude Code CLI through `claude_cli.run_claude` instead and asks for the tool's JSON
as plain text; the reply is validated in code and wrapped in a response-like object
so the rest of the scorer is unchanged.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC
from typing import Any

import anthropic

import claude_cli
from db import Cluster, Database, Score
from filter.prefilter import cluster_text
from ingest.base import utcnow
from score import rubric

log = logging.getLogger(__name__)

RETRYABLE = (
    anthropic.RateLimitError,
    anthropic.APIConnectionError,
    anthropic.InternalServerError,
    claude_cli.ClaudeCliError,
)

HEADLESS_FORMAT_NOTE = (
    "\n\nYou are running without tool calling. Instead of calling the `{tool}` tool, reply "
    "with ONLY a JSON object (no prose, no code fence) that is exactly the tool's input: "
    "it must validate against this JSON schema:\n{schema}"
)


class ScoringError(RuntimeError):
    pass


class HeadlessToolUse:
    """Mimics the SDK's ToolUseBlock for extract_scores()."""

    type = "tool_use"

    def __init__(self, name: str, input: dict[str, Any]):
        self.name = name
        self.input = input


class HeadlessResponse:
    """What create_message returns on the claude_code backend. raw_json() stores the
    verbatim CLI text next to the parsed scores, like the API path stores the SDK dump."""

    stop_reason = "tool_use"

    def __init__(self, text: str, data: dict[str, Any], model: str):
        self.text = text
        self.model = model
        self.content = [HeadlessToolUse(rubric.TOOL_NAME, data)]

    def model_dump_json(self) -> str:
        return json.dumps(
            {"backend": claude_cli.CLAUDE_CODE, "model": self.model, "text": self.text}
        )


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
        self.backend = claude_cli.llm_backend(cfg)
        self._client = client

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic()
        return self._client

    # ---- network ----------------------------------------------------------
    def create_message(self, user_content: str) -> Any:
        if self.backend == claude_cli.CLAUDE_CODE:
            return self.create_message_headless(user_content)
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

    def headless_system_prompt(self) -> str:
        return self.system_prompt + HEADLESS_FORMAT_NOTE.format(
            tool=rubric.TOOL_NAME, schema=json.dumps(rubric.TOOL["input_schema"])
        )

    def create_message_headless(self, user_content: str) -> HeadlessResponse:
        """claude_code backend: no tool schema is enforced server-side, so the JSON reply is
        checked here. A malformed reply is a ScoringError (not retried: the batch is logged
        and skipped); a CLI failure is a ClaudeCliError (retried like an API error)."""
        text = claude_cli.run_claude(
            user_content, system=self.headless_system_prompt(), model=self.model, cfg=self.cfg
        )
        try:
            data = _parse_json_object(text)
        except json.JSONDecodeError as exc:
            raise ScoringError(f"claude_code reply is not JSON: {text[:200]!r}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("scores"), list):
            raise ScoringError("claude_code reply lacks a 'scores' list")
        return HeadlessResponse(text, data, self.model)

    def create_with_retry(self, user_content: str) -> Any:
        for attempt in range(self.max_retries + 1):
            try:
                return self.create_message(user_content)
            except claude_cli.ClaudeCliUnavailable:
                raise  # not installed / cannot start: retrying cannot help
            except RETRYABLE as exc:
                if attempt >= self.max_retries:
                    raise
                delay = self.backoff * (2**attempt)
                log.warning(
                    "API error (%s: %s); retry %d/%d in %.1fs",
                    type(exc).__name__,
                    str(exc)[:200],
                    attempt + 1,
                    self.max_retries,
                    delay,
                )
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
            out.append(
                {
                    "cluster_id": cl.id,
                    "source": ", ".join(sources),
                    "title": title,
                    "abstract": abstract[: self.abstract_max_chars],
                    "doi": doi,
                    "published_at": cl.published_at.isoformat() if cl.published_at else None,
                }
            )
        return out

    def score_batch(self, db: Database, clusters: list[Cluster]) -> list[Score]:
        payload = self.batch_payload(db, clusters)
        response = self.create_with_retry(rubric.build_user_message(payload))
        raw = self.raw_json(response)
        by_index = {int(s["index"]): s for s in self.extract_scores(response)}
        now = utcnow().astimezone(UTC)
        out: list[Score] = []
        for i, p in enumerate(payload):
            s = by_index.get(i)
            if s is None:
                log.warning("cluster %s missing from model output; skipping", p["cluster_id"])
                continue
            score = Score(
                cluster_id=p["cluster_id"],
                model=self.model,
                prompt_version=rubric.PROMPT_VERSION,
                novelty=int(s["novelty"]),
                clinical_significance=int(s["clinical_significance"]),
                audience_interest=int(s["audience_interest"]),
                expertise_fit=int(s["expertise_fit"]),
                timeliness=int(s["timeliness"]),
                evidence_level=str(s["evidence_level"]),
                hype_risk=int(s["hype_risk"]),
                total=rubric.compute_total(s),
                rationale=str(s["rationale"])[:300],
                suggested_angle=str(s["suggested_angle"]),
                raw_response=raw,
                scored_at=now,
            )
            db.insert_score(score)
            out.append(score)
            log.debug(
                "scored cluster %s total=%d: %s", score.cluster_id, score.total, p["title"][:70]
            )
        return out

    def score_unscored(self, db: Database, limit: int | None = None) -> list[Score]:
        clusters = db.unscored_clusters(self.model, rubric.PROMPT_VERSION)
        if limit is not None:
            clusters = clusters[:limit]
        log.info(
            "scoring %d clusters with %s (%s) in batches of %d",
            len(clusters),
            self.model,
            rubric.PROMPT_VERSION,
            self.batch_size,
        )
        scores: list[Score] = []
        total_batches = (len(clusters) + self.batch_size - 1) // self.batch_size
        for i in range(0, len(clusters), self.batch_size):
            batch = clusters[i : i + self.batch_size]
            started = time.monotonic()
            log.info(
                "batch %d/%d: scoring %d clusters",
                i // self.batch_size + 1,
                total_batches,
                len(batch),
            )
            try:
                got = self.score_batch(db, batch)
                scores.extend(got)
                log.info(
                    "batch %d/%d: scored %d of %d clusters in %.0fs",
                    i // self.batch_size + 1,
                    total_batches,
                    len(got),
                    len(batch),
                    time.monotonic() - started,
                )
            except claude_cli.ClaudeCliUnavailable as exc:
                log.error("stopping: %s", exc)
                break
            except (ScoringError, anthropic.APIError, claude_cli.ClaudeCliError) as exc:
                log.error("batch %d-%d failed: %s", i, i + len(batch), exc)
        log.info("scored %d clusters", len(scores))
        return scores


def _parse_json_object(text: str) -> Any:
    """Parse a JSON object, tolerating a ```json fence or stray prose around it."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[4:] if text.lower().startswith("json") else text
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    return json.loads(text)
