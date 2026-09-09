"""Configuration loading. Everything the pipeline does is driven by config.yaml."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"


def load_config(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Load config.yaml, load .env, and expand company feeds into rss sources."""
    load_dotenv()
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(cfg_path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    cfg.setdefault("sources", [])
    cfg["sources"] = list(cfg["sources"]) + _company_sources(cfg.get("companies") or {})
    names = [s["name"] for s in cfg["sources"]]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ValueError(f"duplicate source names in config: {sorted(dupes)}")
    return cfg


def _company_sources(companies: dict[str, Any]) -> list[dict[str, Any]]:
    cadence = int(companies.get("cadence_minutes", 120))
    out = []
    for feed in companies.get("feeds") or []:
        out.append(
            {
                "name": f"company_{feed['key']}",
                "type": "rss",
                "url": feed["url"],
                "cadence_minutes": feed.get("cadence_minutes", cadence),
                "label": feed.get("name", feed["key"]),
                "kind": "company_pr",
            }
        )
    return out


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )
    # httpx logs every request at INFO; keep that at DEBUG-only.
    logging.getLogger("httpx").setLevel(logging.DEBUG if verbose else logging.WARNING)
