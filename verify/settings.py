"""Load verify/config.yaml (this step's own settings; the root config.yaml still names the
LLM backend and the company feeds)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH = Path(__file__).with_name("config.yaml")

DEFAULTS: dict[str, Any] = {
    "model": "",
    "effort": "medium",
    "max_claims_per_draft": 6,
    "max_searches_per_claim": 5,
    "timeout_seconds": 240,
    "trusted_domains": [],
    "tables": {"enabled": True, "max_cells_per_draft": 30, "min_supported_ratio": 0.6},
    "auto_revise": {"enabled": False, "max_rounds": 3, "max_rounds_per_draft": 6},
}


@lru_cache(maxsize=1)
def load_verify_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    out = dict(DEFAULTS)
    if path.exists():
        with open(path, encoding="utf-8") as fh:
            out.update(yaml.safe_load(fh) or {})
    out["trusted_domains"] = [str(d).lower().strip() for d in out.get("trusted_domains") or []]
    out["tables"] = {**DEFAULTS["tables"], **(out.get("tables") or {})}
    out["auto_revise"] = {**DEFAULTS["auto_revise"], **(out.get("auto_revise") or {})}
    return out
