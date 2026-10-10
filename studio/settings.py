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
        "max_waiting": 2,
        "checkpoint": False,
    },
    "manual": {"checkpoint": True},
    "workspace_dir": "studio_pieces",
    "timeouts": {"research": 150, "write": 180, "polish": 60, "revise": 120},
    "max_turns": {"research": 0, "write": 0, "polish": 40, "revise": 0},
    "stage_effort": {"research": "", "write": "", "polish": "", "revise": ""},
    "reference_stage": "research",
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
    "stage_effort",
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
# When the session first reads the reference pieces: research (shipped), so the fact base
# follows their handoff docs, or write, so research's many turns do not carry them.
REFERENCE_STAGES = ("research", "write")

# Side-by-side trials of the usage savings (`run_studio.py --topic T --trial NAME`): a
# piece started as one runs with these settings over studio/config.yaml's, recorded in its
# meta (`trial`), so the editor can write the same topic each way and compare blind.
TRIALS: dict[str, dict[str, Any]] = {
    "current": {},
    "polish": {"stage_effort": {"polish": "high"}},
    "all": {"stage_effort": {"polish": "high"}, "reference_stage": "write"},
}


def load_studio_config(path: str | Path | None = None) -> dict[str, Any]:
    """studio/config.yaml with every key present (missing keys take DEFAULTS)."""
    with open(path or CONFIG_PATH, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ValueError("studio/config.yaml must be a mapping")
    cfg = copy.deepcopy(DEFAULTS)
    for key, value in raw.items():
        if key in _SECTIONS:
            cfg[key] = {**cfg[key], **(value or {})}
        else:
            cfg[key] = value
    cfg["model"] = str(cfg.get("model") or "").strip()
    if not cfg["model"]:
        raise ValueError("studio/config.yaml must name the writer's model (model: ...)")
    cfg["max_parallel"] = max(1, int(cfg.get("max_parallel") or 1))
    cfg["reference_stage"] = str(cfg.get("reference_stage") or "research").strip().lower()
    if cfg["reference_stage"] not in REFERENCE_STAGES:
        raise ValueError(f"studio/config.yaml reference_stage must be one of {REFERENCE_STAGES}")
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


def stage_timeout(cfg: dict[str, Any], stage: str) -> float | None:
    """Seconds for one stage's CLI run, or None for no limit."""
    minutes = float((cfg.get("timeouts") or {}).get(stage) or 0)
    return minutes * 60 if minutes > 0 else None


def stage_max_turns(cfg: dict[str, Any], stage: str) -> int | None:
    turns = int((cfg.get("max_turns") or {}).get(stage) or 0)
    return turns if turns > 0 else None


def for_piece(cfg: dict[str, Any], meta: dict[str, Any] | None) -> dict[str, Any]:
    """The settings one piece runs with: studio/config.yaml's, with its trial's on top
    (`meta["trial"]`, a key of TRIALS; an unknown or missing one changes nothing)."""
    trial = TRIALS.get(str((meta or {}).get("trial") or ""))
    if not trial:
        return cfg
    out = dict(cfg)
    for key, value in trial.items():
        out[key] = {**(cfg.get(key) or {}), **value} if isinstance(value, dict) else value
    return out


def stage_effort(cfg: dict[str, Any], stage: str, default: str | None) -> str | None:
    """The effort one stage runs at: `stage_effort.<stage>` when set, else `default` (the
    piece's own, the top-level `effort:` it was started with)."""
    own = str((cfg.get("stage_effort") or {}).get(stage) or "").strip()
    return own or default


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
