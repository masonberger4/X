"""Loads studio/config.yaml (the studio's own settings; not the root config.yaml) and
knows where the studio's files are: the brief, the angle library, the reference pieces,
the fonts (all shipped with the code) and the workspace and playbook (in the data dir)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

STUDIO_DIR = Path(__file__).resolve().parent
CONFIG_PATH = STUDIO_DIR / "config.yaml"
BRIEF_DIR = STUDIO_DIR / "brief"
ANGLES_PATH = STUDIO_DIR / "angles.yaml"
EXEMPLARS_DIR = STUDIO_DIR / "exemplars"
DEFAULT_PLAYBOOK = STUDIO_DIR / "playbook.md"
PLAYBOOK_NAME = "studio_playbook.md"  # the editable copy, in the data dir

STAGES = ("research", "write", "polish", "revise")

# No model here: the writer's model ID lives only in studio/config.yaml (`model:`), as every
# model ID lives in config, never in code.
DEFAULTS: dict[str, Any] = {
    "effort": "max",
    "max_parallel": 3,
    "auto": {
        "enabled": True,
        "max_new_per_day": 1,
        "min_hours_between": 6,
        "checkpoint": False,
    },
    "manual": {"checkpoint": False},
    "workspace_dir": "studio_pieces",
    "timeouts": {"research": 150, "write": 180, "polish": 60, "revise": 120},
    "max_turns": {"research": 0, "write": 0, "polish": 40, "revise": 0},
    "max_polish_rounds": 3,
    "tools": ["Read", "Write", "Edit", "Glob", "Grep", "WebSearch", "WebFetch", "Agent"],
    "cli_flags": ["--safe-mode", "--restricted", "--permission-mode", "dontAsk"],
    "topics": {"lookback_hours": 48, "shortlist": 8, "min_score": 30, "avoid_days": 10},
    "variety": {"avoid_recent_angles": 3, "avoid_recent_hooks": 2, "recent_pieces_shown": 8},
    "voices": {
        "enabled": True,
        "lean_min_measured": 30,
        "first_person_every_chars": 1200,
        "first_person_max": 6,
    },
    "x": {
        "long_post_max": 25000,
        "thread_post_max": 25000,
        "short_post_max": 1000,
        "headroom": 50,
        "max_cards_total": 4,
    },
    "render": {"browser": "", "timeout_seconds": 60},
    # The piece's steps (studio/steps.py): the effort of each step the writer's session
    # runs, and the model, effort, tools and limits of each step the app runs on its own.
    # Blank effort = the writer's `effort:`; blank model = the writer's `model:`.
    "steps": {
        "enabled": False,
        "parallel": 6,
        "plan": {"effort": ""},
        "factbase": {"effort": ""},
        "post": {"effort": ""},
        "settle": {"effort": ""},
        "polish": {"effort": ""},
        "revise": {"effort": ""},
        "gather_light": {
            "model": "",
            "effort": "",
            "tools": ["WebSearch", "WebFetch"],
            "max_turns": 8,
            "timeout_minutes": 15,
        },
        "gather_deep": {
            "model": "",
            "effort": "",
            "tools": ["WebSearch", "WebFetch"],
            "max_turns": 12,
            "timeout_minutes": 20,
        },
        "claim_list": {
            "model": "",
            "effort": "",
            "tools": [],
            "max_turns": 3,
            "timeout_minutes": 10,
        },
        "claim_check": {
            "model": "",
            "effort": "",
            "tools": ["WebSearch", "WebFetch"],
            "max_turns": 15,
            "timeout_minutes": 20,
            "batch": 10,
        },
    },
    "radar": {
        "enabled": True,
        "scan_every_hours": 20,
        "model": "",
        "effort": "",
        "timeout_minutes": 40,
        "max_topics": 5,
        "max_catalysts": 40,
        "topic_days": 3,
        "calendar_days": 180,
        "past_days": 14,
        "brief_upcoming_days": 21,
        "brief_recent_days": 3,
        "feed_stories": 10,
        "repeat_days": 30,
    },
    "learn": {
        "enabled": True,
        "kpi": "conversation",
        "horizon_hours": 48,
        "baseline_days": 30,
        "min_baseline_posts": 3,
        "smoothing": 1.0,
        "lean_min_measured": 3,
        "prior_sd": 0.5,
        "post_sd": 0.8,
        "playbook": "propose",
        "rewrite_min_new": 3,
        "rewrite_min_hours": 24,
        "model": "",
        "effort": "",
        "max_words": 900,
        "timeout_minutes": 30,
    },
}

_SECTIONS = (
    "auto",
    "manual",
    "timeouts",
    "max_turns",
    "topics",
    "variety",
    "voices",
    "x",
    "render",
    "radar",
    "learn",
)
# learn.playbook: what a learned rewrite does. propose (shipped): it waits on the
# performance page for the editor to apply; auto: the sessions read it at once; off: the
# playbook is never rewritten (the evidence and the lean still reach the prompts).
PLAYBOOK_MODES = ("auto", "propose", "off")


def load_studio_config(path: str | Path | None = None) -> dict[str, Any]:
    """studio/config.yaml with every key present (missing keys take DEFAULTS)."""
    with open(path or CONFIG_PATH, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ValueError("studio/config.yaml must be a mapping")
    cfg = copy.deepcopy(DEFAULTS)
    for key, value in raw.items():
        if key == "steps":
            cfg[key] = _merge_steps(cfg[key], value or {})
        elif key in _SECTIONS:
            cfg[key] = {**cfg[key], **(value or {})}
        else:
            cfg[key] = value
    cfg["model"] = str(cfg.get("model") or "").strip()
    if not cfg["model"]:
        raise ValueError("studio/config.yaml must name the writer's model (model: ...)")
    cfg["max_parallel"] = max(1, int(cfg.get("max_parallel") or 1))
    cfg["tools"] = [str(t) for t in (cfg.get("tools") or [])]
    cfg["cli_flags"] = [str(f) for f in (cfg.get("cli_flags") or [])]
    radar = cfg["radar"]
    # The scan runs on the writer's model and effort unless told otherwise.
    radar["model"] = str(radar.get("model") or "").strip() or cfg["model"]
    radar["effort"] = str(radar.get("effort") or "").strip() or str(cfg.get("effort") or "")
    learn = cfg["learn"]
    from feedback.models import KPIS

    learn["kpi"] = str(learn.get("kpi") or "conversation").strip()
    if learn["kpi"] not in KPIS:
        raise ValueError(f"studio/config.yaml learn.kpi must be one of {KPIS}")
    learn["playbook"] = str(learn.get("playbook") or "off").strip().lower()
    if learn["playbook"] not in PLAYBOOK_MODES:
        raise ValueError(f"studio/config.yaml learn.playbook must be one of {PLAYBOOK_MODES}")
    # The rewrite runs on the writer's model and effort unless told otherwise.
    learn["model"] = str(learn.get("model") or "").strip() or cfg["model"]
    learn["effort"] = str(learn.get("effort") or "").strip() or str(cfg.get("effort") or "")
    return cfg


# The steps the writer's own session runs (one model all the way: only the effort can
# differ) and the steps the app runs as calls of their own (any model).
SESSION_STEPS = ("plan", "factbase", "post", "settle", "polish", "revise")
APP_STEPS = ("gather_light", "gather_deep", "claim_list", "claim_check")


def _merge_steps(base: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("studio/config.yaml steps: must be a mapping")
    out = copy.deepcopy(base)
    for key, value in raw.items():
        if isinstance(out.get(key), dict):
            if not isinstance(value, dict):
                raise ValueError(f"studio/config.yaml steps.{key} must be a mapping")
            out[key] = {**out[key], **value}
        else:
            out[key] = value
    return out


def piece_steps(cfg: dict[str, Any]) -> dict[str, Any]:
    """The step settings a new piece is written with, every blank filled in from the
    writer's model and effort. Recorded in the piece's meta when it starts (runner.new_piece),
    so a piece keeps the settings it began with and every draft can be traced to the model
    and effort of each step that made it."""
    steps = cfg.get("steps") or {}
    model = str(cfg.get("model") or "")
    effort = str(cfg.get("effort") or "")
    out: dict[str, Any] = {
        "enabled": bool(steps.get("enabled")),
        "parallel": max(1, int(steps.get("parallel") or 1)),
    }
    for name in SESSION_STEPS:
        own = steps.get(name) or {}
        out[name] = {"model": model, "effort": str(own.get("effort") or "").strip() or effort}
    for name in APP_STEPS:
        own = dict(steps.get(name) or {})
        own["model"] = str(own.get("model") or "").strip() or model
        own["effort"] = str(own.get("effort") or "").strip()
        own["tools"] = [str(t) for t in (own.get("tools") or [])]
        own["max_turns"] = max(0, int(own.get("max_turns") or 0))
        own["timeout_minutes"] = max(0.0, float(own.get("timeout_minutes") or 0))
        if name == "claim_check":
            own["batch"] = max(1, int(own.get("batch") or 10))
        out[name] = own
    return out


def stage_timeout(cfg: dict[str, Any], stage: str) -> float | None:
    """Seconds for one stage's CLI run, or None for no limit."""
    minutes = float((cfg.get("timeouts") or {}).get(stage) or 0)
    return minutes * 60 if minutes > 0 else None


def stage_max_turns(cfg: dict[str, Any], stage: str) -> int | None:
    turns = int((cfg.get("max_turns") or {}).get(stage) or 0)
    return turns if turns > 0 else None


def workspace_root(data_dir: Path, cfg: dict[str, Any]) -> Path:
    root = Path(str(cfg.get("workspace_dir") or "studio_pieces"))
    return root if root.is_absolute() else data_dir / root


def playbook_path(data_dir: Path) -> Path:
    """The playbook the sessions read: the data dir's editable copy when there is one,
    else the shipped seed (studio/playbook.md)."""
    local = data_dir / PLAYBOOK_NAME
    return local if local.is_file() else DEFAULT_PLAYBOOK


def read_brief(name: str) -> str:
    return (BRIEF_DIR / name).read_text(encoding="utf-8")
