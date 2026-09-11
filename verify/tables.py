"""Pure logic for a draft's comparison table (draft/chart.py:Table): which cells the
source article already backs, how a cell reads as a claim for the fact-checker, and the
decision once every cell has a verdict. No DB, no network; run_verify.py drives it.

Rules (verify/config.yaml `tables`):
- a cell found verbatim in the source article counts as supported by the article;
- any other cell is one web-verified claim ("<row label>, <column>: <cell>");
- a contradicted cell drops the whole table (a picture with a wrong fact is unusable);
- a cell that is unverified, or supported only by an untrusted host, is blanked;
- a row whose label is blanked is removed; fewer than TABLE_MIN_ROWS rows left, or fewer
  than `min_supported_ratio` of the fact cells supported, drops the table;
- more than `max_cells_per_draft` cells drops the table before any call is made.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from draft.chart import TABLE_MIN_ROWS, Table
from verify.verifier import CONTRADICTED, SUPPORTED

PENDING = "pending"
RENDER = "render"
DROP = "drop"

SOURCE_MODEL = "source"  # `model` of a check backed by the source article, not the web


@dataclass
class CellVerdict:
    row: int
    col: int
    verdict: str
    trusted: bool


@dataclass
class TableDecision:
    status: str  # PENDING (cells unchecked), RENDER (with blanked cells), DROP (with reason)
    blanked: frozenset[tuple[int, int]] = frozenset()
    reason: str = ""
    unchecked: list[tuple[int, int]] = field(default_factory=list)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def source_backed_cells(table: Table, source_text: str) -> list[tuple[int, int]]:
    """Cells whose exact text (case and spacing aside) appears in the source article, so
    they need no web search. A bare number counts only as a whole token."""
    src = _norm(source_text)
    out = []
    for r, c, cell in table.cells():
        needle = _norm(cell)
        if len(needle) < 2:
            continue
        if re.fullmatch(r"[\d.,%]+", needle):
            found = re.search(rf"(?<![\w.]){re.escape(needle)}(?![\w.])", src) is not None
        else:
            found = needle in src
        if found:
            out.append((r, c))
    return out


def cell_claim(table: Table, row: int, col: int) -> str:
    """The claim the fact-checker verifies for one cell, with the row label and header so
    it stands on its own: 'Agenus, Most advanced stage: Phase 2'."""
    label = table.rows[row][0]
    header = table.columns[col]
    cell = table.rows[row][col]
    if col == 0:
        return f"{header}: {cell} (as named in the table '{table.title}')"
    return f"{label}, {header}: {cell}"


def decide(
    table: Table,
    verdicts: list[CellVerdict],
    *,
    min_supported_ratio: float,
    max_cells: int,
) -> TableDecision:
    cells = table.cells()
    if len(cells) > max_cells:
        return TableDecision(DROP, reason=f"{len(cells)} cells, cap is {max_cells}")
    by_pos = {(v.row, v.col): v for v in verdicts}
    unchecked = [(r, c) for r, c, _ in cells if (r, c) not in by_pos]
    if unchecked:
        return TableDecision(PENDING, unchecked=unchecked)
    contradicted = [v for v in verdicts if v.verdict == CONTRADICTED]
    if contradicted:
        v = contradicted[0]
        return TableDecision(DROP, reason=f"cell contradicted: {cell_claim(table, v.row, v.col)}")
    blanked = {(v.row, v.col) for v in verdicts if not (v.verdict == SUPPORTED and v.trusted)}
    rows_left = [r for r in range(len(table.rows)) if (r, 0) not in blanked]
    if len(rows_left) < TABLE_MIN_ROWS:
        return TableDecision(DROP, reason="too few rows with a verified label")
    facts = [(r, c) for r, c, _ in cells if c > 0 and r in rows_left]
    supported = [p for p in facts if p not in blanked]
    if facts and len(supported) / len(facts) < min_supported_ratio:
        return TableDecision(
            DROP,
            reason=f"only {len(supported)} of {len(facts)} cells verified "
            f"(need {min_supported_ratio:.0%})",
        )
    return TableDecision(RENDER, blanked=frozenset(blanked))


def apply_row_drops(table: Table, blanked: frozenset[tuple[int, int]]) -> Table:
    """The table to draw: rows whose label was blanked are removed and the blanked set is
    re-indexed; returned as a new Table with the surviving cells."""
    keep = [r for r in range(len(table.rows)) if (r, 0) not in blanked]
    rows = [
        ["" if (r, c) in blanked else cell for c, cell in enumerate(table.rows[r])] for r in keep
    ]
    return Table(title=table.title, columns=list(table.columns), rows=rows, note=table.note)
