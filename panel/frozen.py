"""Where things are when the panel runs as a PyInstaller build, and what to bundle.

From a checkout nothing here changes anything: the data directory is the repo root and
steps run under the current interpreter. In the desktop build (`deploy/desktop.spec`):

- the package files PyInstaller unpacked live in the bundle dir (`_internal/`), and every
  settings file is shipped there at its usual relative path so `Path(__file__)`-based
  lookups in each step keep working unchanged;
- the database, `.env`, lock file and backups live beside the executable (the data
  dir), which the launcher makes the working directory;
- there is no python.exe, so the runs page launches steps with the console build
  (`pipeline-cli`) sitting next to the windowed one, and `pipeline_cli.py` maps
  `run_ingest.py` back to its module.

`bundle_manifest` is the single list of what a build must carry; the spec reads it and
a test checks it against the files on disk so a new settings file cannot be forgotten.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Every project CLI the console build can run, by script name. pipeline_cli.py dispatches
# on this list and the spec bundles each as a hidden import plus a marker file.
CLIS = (
    "run_ingest",
    "run_score",
    "digest",
    "run_draft",
    "run_verify",
    "run_queue",
    "run_app",
    "run_publish",
    "run_feedback",
    "run_ops",
)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path | None:
    """The folder PyInstaller unpacked the package files into; None from a checkout."""
    path = getattr(sys, "_MEIPASS", None)
    return Path(path) if path else None


def data_dir() -> Path:
    """Where pipeline.db, .env, the lock and backups live."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return REPO_ROOT


def step_interpreter() -> str:
    """What the runs page substitutes for "python" in a step's argv."""
    if not is_frozen():
        return sys.executable
    exe = Path(sys.executable)
    return str(exe.with_name(f"pipeline-cli{exe.suffix}"))


def bundle_manifest(root: Path = REPO_ROOT) -> tuple[list[tuple[str, str]], list[str]]:
    """(datas, hiddenimports) for the PyInstaller spec.

    datas are (source path, destination folder inside the bundle) pairs: the root
    config.yaml, every step's own config.yaml, the voice guide, both template folders,
    and a copy of each CLI script so `ops/runner.py`'s existence check passes.
    """
    datas: list[tuple[str, str]] = [(str(root / "config.yaml"), ".")]
    for cfg in sorted(root.glob("*/config.yaml")):
        datas.append((str(cfg), cfg.parent.name))
    datas.append((str(root / "draft" / "voice.md"), "draft"))
    for pkg in ("approval_queue", "panel"):
        datas.append((str(root / pkg / "templates"), f"{pkg}/templates"))
    for cli in CLIS:
        datas.append((str(root / f"{cli}.py"), "."))
    hidden = [*CLIS, "panel.app", "claude_cli"]
    # uvicorn resolves these by name at start-up; PyInstaller cannot see them.
    hidden += [
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
    ]
    return datas, hidden
