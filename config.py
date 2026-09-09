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
    cfg["sources"] = (
        list(cfg["sources"])
        + _company_sources(cfg.get("companies") or {})
        + _conference_sources(cfg.get("conferences") or {}, cfg.get("crossref") or {})
        + _kol_sources(cfg.get("kol") or {})
    )
    names = [s["name"] for s in cfg["sources"]]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ValueError(f"duplicate source names in config: {sorted(dupes)}")
    return cfg


def _company_sources(companies: dict[str, Any]) -> list[dict[str, Any]]:
    cadence = int(companies.get("cadence_minutes", 120))
    out = []
    for feed in companies.get("feeds") or []:
        src = {
            "name": f"company_{feed['key']}",
            "type": "rss",
            "url": feed["url"],
            "cadence_minutes": feed.get("cadence_minutes", cadence),
            "label": feed.get("name", feed["key"]),
            "kind": "company_pr",
        }
        _copy_enabled(feed, src)
        out.append(src)
    return out


def _copy_enabled(src_cfg: dict[str, Any], out: dict[str, Any]) -> None:
    """Carry an explicit `enabled:` through expansion so a deliberately disabled
    source is visible as such (and not reported as "never ran")."""
    if "enabled" in src_cfg:
        out["enabled"] = bool(src_cfg["enabled"])


def _conference_sources(conf: dict[str, Any], crossref: dict[str, Any]) -> list[dict[str, Any]]:
    """`conferences.meetings` -> conf_<key>_abstracts (crossref) [+ conf_<key>_news (rss)]."""
    default_cadence = int(conf.get("default_cadence_minutes", 1440))
    window_cadence = int(conf.get("window_cadence_minutes", 60))
    news_cadence = int(conf.get("news_cadence_minutes", default_cadence))
    out = []
    for m in conf.get("meetings") or []:
        key = m["key"]
        windows = [
            {
                "start": w["start"],
                "end": w["end"],
                "cadence_minutes": int(w.get("cadence_minutes", window_cadence)),
            }
            for w in m.get("windows") or []
        ]
        src: dict[str, Any] = {
            "name": f"conf_{key}_abstracts",
            "type": "crossref",
            "kind": "conference",
            "label": m.get("name", key),
            "meeting": m.get("name", key),
            "journal": m.get("journal"),
            "issn": m["issn"],
            "issue_pattern": m.get("issue_pattern"),
            "boost_patterns": list(m.get("boost_patterns", conf.get("boost_patterns") or [])),
            "lookback_days": int(m.get("lookback_days", conf.get("lookback_days", 3))),
            "rows": int(m.get("rows", crossref.get("rows", 100))),
            "max_pages": int(m.get("max_pages", crossref.get("max_pages", 5))),
            "max_items_per_run": int(m.get("max_items_per_run", conf.get("max_items_per_run", 40))),
            "cadence_minutes": int(m.get("cadence_minutes", default_cadence)),
            "windows": windows,
        }
        keywords = m.get("keywords", conf.get("keywords"))
        if keywords is not None:
            src["keywords"] = list(keywords)
        _copy_enabled(m, src)
        out.append(src)
        if m.get("news_rss"):
            news: dict[str, Any] = {
                "name": f"conf_{key}_news",
                "type": "rss",
                "kind": "conference_news",
                "label": m.get("name", key),
                "url": m["news_rss"],
                "cadence_minutes": int(m.get("news_cadence_minutes", news_cadence)),
                "windows": windows,
            }
            if "news_enabled" in m:
                news["enabled"] = bool(m["news_enabled"])
            out.append(news)
    return out


def _kol_sources(kol: dict[str, Any]) -> list[dict[str, Any]]:
    """`kol` -> ONE x_list source, disabled unless kol.enabled is true."""
    if not kol:
        return []
    src = {k: v for k, v in kol.items() if k not in ("enabled",)}
    src.update(
        name="kol_x_list", type="x_list", kind="kol", enabled=bool(kol.get("enabled", False))
    )
    return [src]


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )
    # httpx logs every request at INFO; keep that at DEBUG-only.
    logging.getLogger("httpx").setLevel(logging.DEBUG if verbose else logging.WARNING)
