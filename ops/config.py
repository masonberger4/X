"""Load ops/config.yaml (this step's own settings; NOT the root config.yaml)."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml

from ops.autorun import DEFAULT_GRACE_MINUTES, parse_times

DEFAULT_OPS_CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"

_DEFAULTS: dict[str, Any] = {
    "steps": [],
    "lock_path": "./pipeline.lock",
    # Automatic runs while the control panel is open (ops/autorun.py, panel/autorun.py).
    "auto_run_enabled": False,
    "auto_run_times": [],
    "auto_run_steps": [],
    "auto_run_grace_minutes": DEFAULT_GRACE_MINUTES,
    "auto_run_backup_hours": 0,  # 0: automatic runs take no backup
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


# The only keys the control panel writes in this file: the runs page's switch and times.
AUTO_RUN_KEYS = ("auto_run_enabled", "auto_run_times")


def save_auto_run(
    enabled: bool, times: str | list[str], path: str | os.PathLike[str] | None = None
) -> dict[str, Any]:
    """Write the runs page's switch and run times into ops/config.yaml. A line edit of two
    top-level keys: every other line, comment and step stays byte for byte (checked by
    parsing the result before it replaces the file), a missing key is appended, and the
    file is swapped in whole so a reader never sees half of it. Times are validated by
    ops.autorun.parse_times and written quoted, so YAML cannot read 12:00 as a number."""
    values = {"auto_run_enabled": bool(enabled), "auto_run_times": parse_times(times)}
    rendered = {
        "auto_run_enabled": "true" if values["auto_run_enabled"] else "false",
        "auto_run_times": "[" + ", ".join(f'"{t}"' for t in values["auto_run_times"]) + "]",
    }
    target = Path(path) if path else DEFAULT_OPS_CONFIG_PATH
    with open(target, encoding="utf-8", newline="") as fh:  # keep the file's line endings
        old_text = fh.read()
    eol = "\r\n" if "\r\n" in old_text else "\n"
    lines = old_text.splitlines(keepends=True)
    seen: set[str] = set()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        key = next((k for k in AUTO_RUN_KEYS if re.match(rf"^{k}\s*:", line)), None)
        if key is None:
            out.append(line)
            i += 1
            continue
        comment = line.split("#", 1)[1].rstrip("\r\n") if "#" in line else ""
        out.append(f"{key}: {rendered[key]}" + (f"  #{comment}" if comment else "") + eol)
        seen.add(key)
        i = _end_of_value(lines, i + 1)
    if out and not out[-1].endswith(("\n", "\r")):
        out[-1] += eol
    for key in AUTO_RUN_KEYS:
        if key not in seen:
            out.append(f"{key}: {rendered[key]}{eol}")
    new_text = "".join(out)
    try:
        before = yaml.safe_load(old_text) or {}
        after = yaml.safe_load(new_text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(
            f"could not edit {target.name} in place ({exc.__class__.__name__}); write "
            'auto_run_times as one line, e.g. auto_run_times: ["06:00", "18:00"]'
        ) from exc
    for key in AUTO_RUN_KEYS:
        before.pop(key, None)
        got = after.pop(key, None)
        if got != values[key]:
            raise ValueError(f"could not write {key} into {target}")
    if before != after:
        raise ValueError(f"refusing to save: the edit would change more than {AUTO_RUN_KEYS}")
    _replace(target, new_text)
    return values


def _end_of_value(lines: list[str], i: int) -> int:
    """Where the old value of a top-level key ends: past every line that belongs to it.
    That is indented lines, a block list's `- ` items (YAML allows them at column 0), a
    wrapped flow list's closing `]`, and blank or comment lines between such lines; a blank
    or comment line followed by the next key stays."""

    def part_of_value(line: str) -> bool:
        return bool(re.match(r"^(\s+\S|-\s|\]\s*(#.*)?$)", line)) and not line.lstrip().startswith(
            "#"
        )

    j = i
    while j < len(lines):
        if part_of_value(lines[j]):
            j += 1
            i = j
            continue
        if lines[j].strip() == "" or lines[j].lstrip().startswith("#"):
            j += 1  # maybe inside the value; only counts if a value line follows
            continue
        break
    return i


def _replace(target: Path, text: str) -> None:
    """Write `text` to a temp file beside `target` and swap it in. Windows refuses the swap
    while another process has the file open for a moment, so retry briefly."""
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        shutil.copymode(target, tmp)  # mkstemp makes it 0600; keep the file's own mode
        for attempt in range(10):
            try:
                os.replace(tmp, target)
                return
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.1)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
