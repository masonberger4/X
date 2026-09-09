"""Load ops/config.yaml (this step's own settings; NOT the root config.yaml)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

DEFAULT_OPS_CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"

_DEFAULTS: dict[str, Any] = {
    "steps": [],
    "lock_path": "./pipeline.lock",
    "run_log_tail_chars": 4000,
    "health": {},
    "backups": {"dir": "./backups", "keep": 14},
    "alerts": {
        "cooldown_hours": 6,
        "min_status": "warn",
        "channels": {"log": True, "webhook": True, "email": False},
        "webhook_timeout_seconds": 10,
    },
}


def load_ops_config(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    cfg_path = Path(path) if path else DEFAULT_OPS_CONFIG_PATH
    with open(cfg_path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    for key, default in _DEFAULTS.items():
        if isinstance(default, dict):
            merged = dict(default)
            merged.update(cfg.get(key) or {})
            if "channels" in default:
                channels = dict(default["channels"])
                channels.update((cfg.get(key) or {}).get("channels") or {})
                merged["channels"] = channels
            cfg[key] = merged
        else:
            cfg.setdefault(key, default)
    for step in cfg["steps"]:
        step.setdefault("enabled", True)
        step.setdefault("required", False)
        step.setdefault("timeout_seconds", 600)
        if not isinstance(step.get("argv"), list) or not step["argv"]:
            raise ValueError(f"step {step.get('name')!r}: argv must be a non-empty list")
    return cfg
