"""Loads swarm/config.yaml (step 9's own settings; not the root config.yaml)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH = Path(__file__).with_name("config.yaml")

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "model": "",
    "assembler_model": "",
    "fan_out": 6,
    "layers": 2,
    "judge_votes": 3,
    "max_similarity": 0.85,
    "control": {"enabled": True},
    "formats": {"long_max_chars": 4000, "long_section_chars": 700},
    "evolve": {
        "kpi": "impressions",
        "baseline_days": 30,
        "min_baseline_posts": 3,
        "min_posts": 5,
        "min_alive": 2,
        "population_size": 3,
        "designer_population_size": 6,
        "mutation_model": "",
        "format_min_posts": 8,
        "format_population_size": 5,
    },
}


def load_swarm_config(path: str | Path | None = None) -> dict[str, Any]:
    """Return swarm/config.yaml with every key present (missing keys take DEFAULTS)."""
    with open(path or CONFIG_PATH, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    cfg = {**DEFAULTS, **raw}
    cfg["control"] = {**DEFAULTS["control"], **(raw.get("control") or {})}
    cfg["evolve"] = {**DEFAULTS["evolve"], **(raw.get("evolve") or {})}
    cfg["formats"] = {**DEFAULTS["formats"], **(raw.get("formats") or {})}
    return cfg
