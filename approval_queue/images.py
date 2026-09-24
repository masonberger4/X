"""Renders a draft's chart or table to its PNG, has the grader score it, and records it.
Fails soft: no matplotlib, a bad spec or a disk error means "draft without an image",
never a lost draft; a grader failure means "keep the render we have".

The render-grade loop (`_render`): draw with the house `Style`, show the PNG to
`draft.grader.grade_image`, and stop at `images.grader.min_score` or after
`images.grader.max_iterations` renders; otherwise apply the grader's knob changes and draw
again. The best-scoring render is the one kept (a later, worse attempt is redrawn over),
and every grade goes to `image_grades` with the kept one flagged.

attach_chart is used by run_draft.py after insert_draft and by the queue's revise route
after store.revise; a Table handed to it is NOT rendered (its cells are not verified yet).
attach_table is used by run_verify.py once every cell has a verdict. A render without an
explicit `style` starts from the Style recorded on the draft (store.set_style, written by
run_draft.py from its designer), so every redraw keeps the designer's look.
Settings: `images` in draft/config.yaml (enabled, grader).
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from approval_queue import store
from draft import branding, grader
from draft.chart import Chart, Style, Table, alt_text, render_chart, render_table
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
    style: Style | None = None,
) -> Path | None:
    """Render `chart` for draft `draft_id`, store its path, return it. None when there is no
    chart, images are disabled, or rendering failed (logged; drafts.image_path stays NULL).
    A Table is left for run_verify.py (attach_table) and returns None. `style` (step 9's
    designer genome) is the Style the first render starts from; the grader loop adjusts
    from there."""
    if chart is None:
        return None
    if isinstance(chart, Table):
        log.info("draft %d: table awaits cell verification (run_verify.py)", draft_id)
        return None
    drawn, logos = _brand_chart(chart)
    return _render(
        conn,
        draft_id,
        drawn,
        lambda path, style: render_chart(
            drawn, path, source_url=source_url, style=style, logos=logos
        ),
        alt_text(drawn, source_url),
        cfg,
        style,
    )


def attach_extra_charts(
    conn: sqlite3.Connection,
    draft_id: int,
    charts: list[Chart],
    *,
    source_url: str = "",
    cfg: dict | None = None,
    style: Style | None = None,
) -> list[Path]:
    """Phase four: render every chart beyond the first visual to draft_<id>_<k>.png through
    the same render-grade loop, recorded at index k in drafts.images_json. Fail-soft per
    picture. Returns the paths that were rendered."""
    out: list[Path] = []
    for k, chart in enumerate(charts, start=1):
        drawn, logos = _brand_chart(chart)
        path = _render(
            conn,
            draft_id,
            drawn,
            lambda path, style, c=drawn, lg=logos: render_chart(
                c, path, source_url=source_url, style=style, logos=lg
            ),
            alt_text(drawn, source_url),
            cfg,
            style,
            index=k,
        )
        if path is not None:
            out.append(path)
    return out


def attach_table(
    conn: sqlite3.Connection,
    draft_id: int,
    table: Table,
    *,
    source_url: str = "",
    blanked: frozenset[tuple[int, int]] = frozenset(),
    cfg: dict | None = None,
    style: Style | None = None,
) -> Path | None:
    """Render a table whose cells step 2b has checked; `blanked` cells are drawn as blanks.
    Company cells get their configured ticker and logo first (draft/branding.py, as bar
    labels do in attach_chart); the alt text describes the branded table so it matches the
    picture."""
    drawn, logos = _brand(table)
    return _render(
        conn,
        draft_id,
        drawn,
        lambda path, style: render_table(
            drawn, path, source_url=source_url, blanked=blanked, style=style, logos=logos
        ),
        alt_text(drawn, source_url, blanked),
        cfg,
        style,
    )


def _branding() -> branding.Branding:
    import config as root_config
    from panel.frozen import data_dir

    return branding.load_branding(root_config.load_config(), data_dir())


def _brand(table: Table) -> tuple[Table, dict]:
    """Tickers and logos from the root config; any failure means the table as it is."""
    try:
        return branding.brand_table(table, _branding())
    except Exception:
        log.exception("table branding skipped")
        return table, {}


def _brand_chart(chart: Chart) -> tuple[Chart, dict[int, Path]]:
    """Tickers and logos on bar labels that name a configured company; any failure means
    the chart as it is."""
    try:
        return branding.brand_chart(chart, _branding())
    except Exception:
        log.exception("chart branding skipped")
        return chart, {}


def _render(
    conn: sqlite3.Connection,
    draft_id: int,
    visual: Chart | Table,
    draw,
    alt: str,
    cfg: dict | None,
    style: Style | None = None,
    index: int = 0,
) -> Path | None:
    if not images_enabled(cfg):
        log.info("draft %d: visual kept as spec only (images disabled in draft/config)", draft_id)
        return None
    path = store.image_file(draft_id, index)
    if style is None:
        # a redraw (a revision, the verifier's table, a scrubbed caption) starts from the
        # Style the draft was first drawn in, its designer's, not the house style
        stored = store.get_style(conn, draft_id)
        style = Style().apply(stored) if stored else Style()
    try:
        draw(path, style)
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
    store.set_image(conn, draft_id, path, alt, index=index)
    if index == 0:
        store.clear_image_grades(conn, draft_id)  # a revise re-renders: earlier grades are stale
    log.info("draft %d: chart rendered to %s", draft_id, path)
    _grade_loop(conn, draft_id, visual, draw, path, style, cfg)
    return path


def _grade_loop(
    conn: sqlite3.Connection,
    draft_id: int,
    visual: Chart | Table,
    draw,
    path: Path,
    style: Style,
    cfg: dict | None,
) -> None:
    """Grade the render at `path`, redraw with the grader's knob changes while the score is
    under min_score and iterations remain, and leave the best-scoring render on disk. Any
    grader failure ends the loop with the current picture kept."""
    settings = grader.grader_settings(cfg if cfg is not None else load_draft_config())
    if not settings.enabled:
        return
    best_score, best_style, best_grade_id = -1, style, None
    previous = None
    for iteration in range(1, settings.max_iterations + 1):
        try:
            grade = grader.grade_image(
                path,
                visual,
                style,
                model=settings.model,
                iteration=iteration,
                previous=previous,
                effort=settings.effort,
            )
        except Exception:  # the grader is advisory: a failed call keeps the render
            log.exception("draft %d: image grader failed (render kept)", draft_id)
            break
        grade_id = store.record_image_grade(
            conn,
            draft_id,
            iteration=iteration,
            score=grade.score,
            flaws=grade.flaws,
            fixes=grade.fixes,
            adjustments=grade.adjustments,
            style=style.to_dict(),
            model=grade.model,
            criteria=grade.criteria,
        )
        if grade.score > best_score:
            best_score, best_style, best_grade_id = grade.score, style, grade_id
        if grade.passed(settings.min_score):
            log.info("draft %d: image scored %d/10, done", draft_id, grade.score)
            break
        adjustments = grade.adjustments or grader.fallback_adjustments(visual, style)
        if iteration == settings.max_iterations or not adjustments:
            log.info(
                "draft %d: image scored %d/10 after %d render(s), keeping the best (%d/10)",
                draft_id,
                grade.score,
                iteration,
                best_score,
            )
            break
        if not grade.adjustments:
            log.info("draft %d: grader sent no adjustments, stepping %s", draft_id, adjustments)
        style = style.apply(adjustments)
        previous = grade
        try:
            draw(path, style)
        except Exception:
            log.exception("draft %d: re-render with grader adjustments failed", draft_id)
            style = best_style
            break
    if best_grade_id is not None:
        store.mark_image_grade_kept(conn, draft_id, best_grade_id)
    if style != best_style:  # the last render was worse than an earlier one: redraw that one
        try:
            draw(path, best_style)
        except Exception:
            log.exception("draft %d: redraw of the best-scoring render failed", draft_id)
