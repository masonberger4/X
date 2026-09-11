"""Renders a draft's chart or table to its PNG and records it. Fails soft: no matplotlib, a
bad spec or a disk error means "draft without an image", never a lost draft.

attach_chart is used by run_draft.py after insert_draft and by the queue's revise route
after store.revise; a Table handed to it is NOT rendered (its cells are not verified yet).
attach_table is used by run_verify.py once every cell has a verdict.
Settings: `images` in draft/config.yaml (enabled).
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from approval_queue import store
from draft.chart import Chart, Table, alt_text, render_chart, render_table
from draft.settings import load_draft_config

log = logging.getLogger(__name__)


def images_enabled(cfg: dict | None = None) -> bool:
    cfg = cfg if cfg is not None else load_draft_config()
    return bool((cfg.get("images") or {}).get("enabled", True))


def attach_chart(
    conn: sqlite3.Connection,
    draft_id: int,
    chart: Chart | Table | None,
    *,
    source_url: str = "",
    cfg: dict | None = None,
) -> Path | None:
    """Render `chart` for draft `draft_id`, store its path, return it. None when there is no
    chart, images are disabled, or rendering failed (logged; drafts.image_path stays NULL).
    A Table is left for run_verify.py (attach_table) and returns None."""
    if chart is None:
        return None
    if isinstance(chart, Table):
        log.info("draft %d: table awaits cell verification (run_verify.py)", draft_id)
        return None
    return _render(
        conn,
        draft_id,
        lambda path: render_chart(chart, path, source_url=source_url),
        alt_text(chart, source_url),
        cfg,
    )


def attach_table(
    conn: sqlite3.Connection,
    draft_id: int,
    table: Table,
    *,
    source_url: str = "",
    blanked: frozenset[tuple[int, int]] = frozenset(),
    cfg: dict | None = None,
) -> Path | None:
    """Render a table whose cells step 2b has checked; `blanked` cells are drawn as blanks."""
    return _render(
        conn,
        draft_id,
        lambda path: render_table(table, path, source_url=source_url, blanked=blanked),
        alt_text(table, source_url, blanked),
        cfg,
    )


def _render(
    conn: sqlite3.Connection, draft_id: int, draw, alt: str, cfg: dict | None
) -> Path | None:
    if not images_enabled(cfg):
        log.info("draft %d: visual kept as spec only (images disabled in draft/config)", draft_id)
        return None
    path = store.image_file(draft_id)
    try:
        draw(path)
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
    store.set_image(conn, draft_id, path, alt)
    log.info("draft %d: chart rendered to %s", draft_id, path)
    return path
