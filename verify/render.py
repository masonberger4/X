"""The table render-or-drop step shared by run_verify.py and the queue's "Trust this
source" button: from the stored cell verdicts (trust re-read against the CURRENT host
list) decide whether the draft's table is drawn, dropped or still waiting, and act on it.
Pure decisions live in verify/tables.py; this module is the one that touches the DB and
the picture."""

from __future__ import annotations

import logging
from typing import Any

from approval_queue import images
from approval_queue import store as queue_store
from verify import store, tables
from verify.verifier import is_trusted

log = logging.getLogger(__name__)


def root_config() -> dict[str, Any]:
    """The root config.yaml, or {} when it is missing or unreadable (company sites then
    do not count as trusted, nothing else changes)."""
    try:
        from config import load_config

        return load_config()
    except Exception:
        return {}


def decide(conn, d, table, *, ratio, max_cells, hosts=None) -> tables.TableDecision:
    """The render/drop decision from the stored cell verdicts. Trust is re-read from each
    verdict's source URL against the CURRENT host list (`hosts`) as well as the flag stored
    at check time, so adding a company to config.yaml makes its already-checked cells
    count without another web call."""
    verdicts = [
        tables.CellVerdict(
            k.row,
            k.col,
            k.verdict,
            k.trusted or (bool(hosts) and is_trusted(k.source_url or "", hosts)),
        )
        for k in store.table_checks_for_draft(conn, d.id)
    ]
    return tables.decide(table, verdicts, min_supported_ratio=ratio, max_cells=max_cells)


def reindex(table, blanked: frozenset[tuple[int, int]]) -> frozenset[tuple[int, int]]:
    """Blanked positions in the drawn table, whose rows with a blanked label are gone."""
    keep = [r for r in range(len(table.rows)) if (r, 0) not in blanked]
    new_row = {r: i for i, r in enumerate(keep)}
    return frozenset((new_row[r], c) for r, c in blanked if r in new_row)


def finalize_table(conn, d, *, cfg: dict, hosts: set[str]) -> tables.TableDecision:
    """Decide and act: a table with every cell checked is drawn to the draft's picture
    (unsupported cells blanked) or dropped on the record; one with an unchecked cell is
    left alone. Returns the decision so the caller can log or display it."""
    table = d.draft.table
    assert table is not None
    tcfg = cfg["tables"]
    ratio = float(tcfg.get("min_supported_ratio", 0.6))
    max_cells = int(tcfg.get("max_cells_per_draft", 30))
    decision = decide(conn, d, table, ratio=ratio, max_cells=max_cells, hosts=hosts)
    if decision.status == tables.PENDING:
        log.info("draft %d: table still has %d unchecked cell(s)", d.id, len(decision.unchecked))
    elif decision.status == tables.DROP:
        log.warning("draft %d: table dropped: %s", d.id, decision.reason)
        queue_store.drop_table(conn, d.id, decision.reason)
    else:
        drawn = tables.apply_row_drops(table, decision.blanked)
        path = images.attach_table(
            conn,
            d.id,
            drawn,
            source_url=d.url or "",
            blanked=reindex(table, decision.blanked),
        )
        log.info(
            "draft %d: table rendered to %s (%d cell(s) blanked)",
            d.id,
            path,
            len(decision.blanked),
        )
    return decision
