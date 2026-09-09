"""Loads draft/config.yaml (step 7's own settings; not the root config.yaml)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH = Path(__file__).with_name("config.yaml")


def load_draft_config(path: str | Path | None = None) -> dict[str, Any]:
    """Return the parsed draft/config.yaml with the `examples` and `report` sections present."""
    with open(path or CONFIG_PATH, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    cfg.setdefault("examples", {})
    cfg.setdefault("report", {})
    return cfg
