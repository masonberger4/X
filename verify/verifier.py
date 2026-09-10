"""Verify one claim against the open web. Pure except for `call_model`, the single network
call, which either runs the Claude Code CLI with WebSearch/WebFetch enabled (claude_code
backend) or the Anthropic API with the server-side web_search tool. Tests replace it.

The verifier never edits a draft. Its output is evidence for the human: a verdict, the
source it found, the sentence it relied on, and a note. A verdict only counts as
"verified" when the source is on a trusted host (verify/config.yaml plus the company
sites in the root config); anything else is a lead.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import claude_cli

log = logging.getLogger(__name__)

SUPPORTED = "supported"
CONTRADICTED = "contradicted"
UNVERIFIED = "unverified"
VERDICTS = (SUPPORTED, CONTRADICTED, UNVERIFIED)

MAX_TOKENS = 4000
MAX_API_TURNS = 4  # pause_turn continuations on the API backend

SYSTEM = """You are a fact-checker for an X account written by a PhD-level immuno-oncology analyst. A draft post contains a claim the writer added from memory, not from the source article. Your job is to find a PRIMARY source on the web that supports or contradicts that claim, and to quote it.

Primary sources, in order of preference: the company's own press release or investor page, ClinicalTrials.gov, the journal article or abstract, an FDA or EMA page, an SEC filing. News articles and social posts are leads, not proof; use them only to find the primary source.

Rules:
- Search the web. Do not rely on memory.
- Quote the sentence that supports or contradicts the claim VERBATIM from the page; do not paraphrase it.
- If the best you can find is partial, ambiguous, or from a secondary source, the verdict is "unverified" and the note says what is missing.
- If the claim is out of date (a trial has since read out, a company has since been acquired), the verdict is "contradicted" with the newer fact in the note.
- Never invent a URL. Only return a URL you actually visited or that a search result returned.

Reply with ONLY a JSON object:
{"verdict": "supported" | "contradicted" | "unverified", "source_url": "<url or empty>", "quote": "<verbatim sentence or empty>", "note": "<one or two sentences>"}"""


@dataclass
class ClaimCheck:
    claim_index: int
    claim: str
    verdict: str
    source_url: str
    quote: str
    note: str
    trusted: bool


def build_user(
    claim: str, *, title: str, url: str, single_post: str, published_at: str | None
) -> str:
    return "\n".join(
        [
            "CLAIM TO CHECK:",
            claim,
            "",
            "CONTEXT (the draft this claim appears in):",
            f"Story: {title}",
            f"Story source: {url}",
            f"Story date: {published_at or 'unknown'}",
            f"Draft post: {single_post}",
            "",
            "Find the primary source and reply with the JSON object only.",
        ]
    )


def parse_reply(text: str) -> dict[str, Any]:
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"no JSON object in reply: {text[:120]!r}")
    data = json.loads(m.group(0))
    verdict = str(data.get("verdict") or "").strip().lower()
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r}")
    return {
        "verdict": verdict,
        "source_url": str(data.get("source_url") or "").strip()[:500],
        "quote": str(data.get("quote") or "").strip()[:600],
        "note": str(data.get("note") or "").strip()[:400],
    }


def host_of(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def trusted_hosts(cfg: dict[str, Any], root_cfg: dict[str, Any] | None = None) -> set[str]:
    """verify/config.yaml trusted_domains plus the host of every company feed in the root
    config, so a company's own press-release page counts."""
    hosts = {host_of("https://" + str(d).lower()) for d in cfg.get("trusted_domains") or []}
    # config.yaml's companies.feeds is a list of {key, name, url} entries (that is what
    # config.load_config expands into rss sources); a {key: url-or-entry} mapping is
    # accepted too so a hand-built config keeps working.
    feeds = ((root_cfg or {}).get("companies") or {}).get("feeds") or []
    entries = feeds.values() if isinstance(feeds, dict) else feeds
    for feed in entries:
        url = feed.get("url") if isinstance(feed, dict) else feed
        if url:
            h = host_of(str(url))
            if h:
                hosts.add(h)
    return {h for h in hosts if h}


def is_trusted(url: str, hosts: set[str]) -> bool:
    h = host_of(url)
    return bool(h) and any(h == t or h.endswith("." + t) for t in hosts)


def call_model(
    system: str, user: str, model: str, effort: str | None, cfg: dict, root_cfg: dict
) -> str:
    """The single network call: Claude with web search, on either backend."""
    if claude_cli.llm_backend(root_cfg) == claude_cli.CLAUDE_CODE:
        merged = dict(root_cfg)
        merged["claude_code"] = dict(root_cfg.get("claude_code") or {})
        merged["claude_code"]["timeout_seconds"] = cfg.get("timeout_seconds", 240)
        return claude_cli.run_claude(
            user,
            system=system,
            model=model,
            cfg=merged,
            effort=effort,
            tools=["WebSearch", "WebFetch"],
        )

    import anthropic  # local import so tests never touch the SDK

    client = anthropic.Anthropic()
    kwargs: dict[str, Any] = dict(
        model=model,
        max_tokens=MAX_TOKENS,
        system=system,
        tools=[
            {
                "type": "web_search_20260209",
                "name": "web_search",
                "max_uses": int(cfg.get("max_searches_per_claim", 5)),
            }
        ],
    )
    if effort:
        kwargs["output_config"] = {"effort": effort}
    messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
    for _ in range(MAX_API_TURNS):
        resp = client.messages.create(messages=messages, **kwargs)
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        return "".join(getattr(b, "text", "") for b in resp.content if b.type == "text")
    raise RuntimeError("verifier did not finish within the turn limit")


CallFn = Callable[[str, str, str, str | None, dict, dict], str]


def verify_claim(
    index: int,
    claim: str,
    *,
    title: str,
    url: str,
    single_post: str,
    published_at: str | None,
    cfg: dict[str, Any],
    root_cfg: dict[str, Any],
    hosts: set[str],
    call: CallFn = call_model,
) -> ClaimCheck:
    model = str(cfg.get("model") or "").strip()
    if not model:
        raise ValueError("verify/config.yaml model is not set")
    effort = str(cfg.get("effort") or "").strip().lower() or None
    user = build_user(
        claim, title=title, url=url, single_post=single_post, published_at=published_at
    )
    data = parse_reply(call(SYSTEM, user, model, effort, cfg, root_cfg))
    trusted = data["verdict"] != UNVERIFIED and is_trusted(data["source_url"], hosts)
    return ClaimCheck(
        claim_index=index,
        claim=claim,
        verdict=data["verdict"],
        source_url=data["source_url"],
        quote=data["quote"],
        note=data["note"],
        trusted=trusted,
    )
