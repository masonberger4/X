"""Renders a draft's chart to its PNG and records it. Fails soft: no matplotlib, a bad spec
or a disk error means "draft without an image", never a lost draft.

Used by run_draft.py after insert_draft and by the queue's revise route after store.revise.
Settings: `images` in draft/config.yaml (enabled).
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from approval_queue import store
from draft.chart import Chart, render_chart
from draft.settings import load_draft_config

log = logging.getLogger(__name__)


def images_enabled(cfg: dict | None = None) -> bool:
    cfg = cfg if cfg is not None else load_draft_config()
    return bool((cfg.get("images") or {}).get("enabled", True))


def attach_chart(
    conn: sqlite3.Connection,
    draft_id: int,
    chart: Chart | None,
    *,
    source_url: str = "",
    cfg: dict | None = None,
) -> Path | None:
    """Render `chart` for draft `draft_id`, store its path, return it. None when there is no
    chart, images are disabled, or rendering failed (logged; drafts.image_path stays NULL)."""
    if chart is None:
        return None
    if not images_enabled(cfg):
        log.info("draft %d: chart kept as spec only (images disabled in draft/config)", draft_id)
        return None
    path = store.image_file(draft_id)
    try:
        render_chart(chart, path, source_url=source_url)
    except ImportError:
        log.warning(
            "draft %d: chart not rendered, matplotlib is not installed "
            "(pip install -e '.[images]')",
            draft_id,
        )
        return None
    except Exception:  # a drawing or disk error must not lose the draft
        log.exception("draft %d: chart rendering failed", draft_id)
        return None
    store.set_image(conn, draft_id, path)
    log.info("draft %d: chart rendered to %s", draft_id, path)
    return path
