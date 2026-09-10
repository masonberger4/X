"""Story linking: one model call per `run_score.py` run that groups clusters reporting
the same news event, so a company release, its wire copy and the trade-press
write-ups become one cluster before scoring.

Title-similarity dedup (`filter/dedup.py`) catches verbatim repeats; it cannot see
that "Kura creates Lilly-backed spinout" and "Kura spins diabetes work into a
Lilly-backed biotech startup" are one story. This pass can. Candidates are every
prefilter-passed cluster created or published in the last `linking.window_hours`,
scored or not, so a write-up arriving the morning after a release joins the
release's (already scored) cluster instead of being scored again.

The model only proposes groups; code validates them (unknown ids, overlaps and
singletons are dropped) and `Database.merge_clusters` moves the rows. A group keeps
the cluster that already has a score (the oldest of them), else the oldest cluster.
The single network call is `score.rater.call_model`; tests pass a fake `call`.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from db import Cluster, Database
from filter.prefilter import cluster_text
from ingest.base import utcnow
from score import rater

log = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "window_hours": 48,
    "max_clusters": 200,
    "abstract_chars": 300,
    "max_tokens": 4000,
}

SYSTEM = """You are a news editor's desk assistant for an immuno-oncology biotech account. You are given a numbered list of recent news clusters (title, source, date, a snippet). Several of them may be different outlets reporting the SAME news event: a company's press release, a wire-service copy of it, and one or more trade-press articles about it, each with its own headline.

Group clusters that report the same underlying event: the same company (or companies) and the same announcement, result, deal, regulatory action, financing or personnel change, within a few days of each other. Two different announcements from the same company are NOT the same event. A journal paper and a press release about that paper ARE the same event. A general roundup that mentions several stories belongs to none of them. When unsure, do not group.

Reply with ONLY a JSON object: {"groups": [[id, id, ...], ...]} listing only groups of two or more ids. Return {"groups": []} when nothing matches."""


CallFn = Callable[..., str]


@dataclass
class LinkResult:
    candidates: int = 0
    groups: list[list[int]] = field(default_factory=list)
    merged: int = 0  # clusters folded into another
    error: str | None = None


def settings(cfg: dict[str, Any]) -> dict[str, Any]:
    out = dict(DEFAULTS)
    out.update(cfg.get("linking") or {})
    return out


def build_entries(db: Database, clusters: list[Cluster], abstract_chars: int) -> list[dict]:
    entries = []
    for cl in clusters:
        items = db.items_in_cluster(cl.id)
        title, abstract = cluster_text(items) if items else (cl.title, "")
        entries.append(
            {
                "id": cl.id,
                "sources": sorted({i.source for i in items}) or ["?"],
                "published_at": cl.published_at.date().isoformat() if cl.published_at else "?",
                "title": title,
                "snippet": abstract[:abstract_chars],
            }
        )
    return entries


def build_user(entries: list[dict]) -> str:
    lines = []
    for e in entries:
        lines.append(
            f"[{e['id']}] ({', '.join(e['sources'])}; {e['published_at']}) {e['title']}\n"
            f"    {e['snippet']}"
        )
    return "CLUSTERS:\n\n" + "\n".join(lines) + "\n\nGroup the same-event clusters. JSON only."


def parse_reply(text: str, valid_ids: set[int]) -> list[list[int]]:
    """Groups of known, non-overlapping ids with two or more members; the rest is dropped."""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"no JSON object in reply: {text[:120]!r}")
    data = json.loads(m.group(0))
    raw = data.get("groups")
    if not isinstance(raw, list):
        raise ValueError("reply has no 'groups' list")
    seen: set[int] = set()
    groups: list[list[int]] = []
    for g in raw:
        if not isinstance(g, list):
            continue
        ids: list[int] = []
        for x in g:
            try:
                i = int(x)
            except (TypeError, ValueError):
                continue
            if i in valid_ids and i not in seen and i not in ids:
                ids.append(i)
        if len(ids) >= 2:
            groups.append(ids)
            seen.update(ids)
    return groups


def choose_keep(db: Database, ids: list[int]) -> int:
    """The scored cluster with the lowest id, else the lowest id."""
    scored = [i for i in ids if db.has_score(i)]
    return min(scored) if scored else min(ids)


def call_model(system: str, user: str, model: str, effort: str | None, cfg: dict) -> str:
    return rater.call_model(
        system, user, model, effort, cfg, max_tokens=int(settings(cfg)["max_tokens"])
    )


def link_recent(db: Database, cfg: dict[str, Any], call: CallFn = call_model) -> LinkResult:
    """Group and merge recent clusters. Never raises: a model or parse failure is
    logged, recorded on the result, and scoring proceeds on the unmerged clusters."""
    st = settings(cfg)
    res = LinkResult()
    if not st.get("enabled", True):
        return res
    model = str((cfg.get("models") or {}).get("linker") or "").strip()
    if not model:
        res.error = "config.yaml models.linker is not set"
        log.warning("linking skipped: %s", res.error)
        return res
    effort = str(st.get("effort") or "").strip().lower() or None
    since = utcnow() - timedelta(hours=int(st["window_hours"]))
    clusters = db.clusters_for_linking(since, int(st["max_clusters"]))
    res.candidates = len(clusters)
    if len(clusters) < 2:
        return res
    entries = build_entries(db, clusters, int(st["abstract_chars"]))
    try:
        reply = call(SYSTEM, build_user(entries), model, effort, cfg)
        res.groups = parse_reply(reply, {e["id"] for e in entries})
    except Exception as exc:  # noqa: BLE001 - fail soft: scoring must go on
        res.error = f"{type(exc).__name__}: {str(exc)[:200]}"
        log.error("linking failed: %s", res.error)
        return res
    for ids in res.groups:
        keep = choose_keep(db, ids)
        others = [i for i in ids if i != keep]
        db.merge_clusters(keep, others)
        res.merged += len(others)
        log.info("linked clusters %s into %s", others, keep)
    log.info(
        "linking: %d candidates, %d groups, %d clusters merged",
        res.candidates,
        len(res.groups),
        res.merged,
    )
    return res
