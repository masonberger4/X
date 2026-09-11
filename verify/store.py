"""Step 2b's own tables, claim_checks and table_checks (one verdict per cell of a draft's
comparison table), in the shared pipeline DB. Drafts are read only through
approval_queue.store (list_drafts / get_draft); this module never touches step 1's tables."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from approval_queue import store as queue_store
from verify.verifier import CONTRADICTED, SUPPORTED, ClaimCheck

_SCHEMA = """
CREATE TABLE IF NOT EXISTS claim_checks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id    INTEGER NOT NULL REFERENCES drafts(id),
    claim_index INTEGER NOT NULL,
    claim       TEXT NOT NULL,
    verdict     TEXT NOT NULL,
    source_url  TEXT NOT NULL DEFAULT '',
    quote       TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    trusted     INTEGER NOT NULL DEFAULT 0,
    model       TEXT NOT NULL,
    checked_at  TEXT NOT NULL,
    UNIQUE(draft_id, claim_index)
);
CREATE TABLE IF NOT EXISTS table_checks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id    INTEGER NOT NULL REFERENCES drafts(id),
    row         INTEGER NOT NULL,
    col         INTEGER NOT NULL,
    cell        TEXT NOT NULL,
    verdict     TEXT NOT NULL,
    source_url  TEXT NOT NULL DEFAULT '',
    quote       TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    trusted     INTEGER NOT NULL DEFAULT 0,
    model       TEXT NOT NULL,
    checked_at  TEXT NOT NULL,
    UNIQUE(draft_id, row, col)
);
"""


@dataclass
class CheckRow:
    id: int
    draft_id: int
    claim_index: int
    claim: str
    verdict: str
    source_url: str
    quote: str
    note: str
    trusted: bool
    model: str
    checked_at: str

    @property
    def label(self) -> str:
        if self.verdict == SUPPORTED:
            return "supported" if self.trusted else "supported (untrusted source)"
        if self.verdict == CONTRADICTED:
            return "contradicted" if self.trusted else "contradicted (untrusted source)"
        return "unverified"


def connect(path=None) -> sqlite3.Connection:
    conn = queue_store.connect(path)
    conn.executescript(_SCHEMA)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


def _row(r: sqlite3.Row) -> CheckRow:
    return CheckRow(
        id=r["id"],
        draft_id=r["draft_id"],
        claim_index=r["claim_index"],
        claim=r["claim"],
        verdict=r["verdict"],
        source_url=r["source_url"],
        quote=r["quote"],
        note=r["note"],
        trusted=bool(r["trusted"]),
        model=r["model"],
        checked_at=r["checked_at"],
    )


def checks_for_draft(conn: sqlite3.Connection, draft_id: int) -> list[CheckRow]:
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT * FROM claim_checks WHERE draft_id = ? ORDER BY claim_index", (draft_id,)
    ).fetchall()
    return [_row(r) for r in rows]


def checks_by_draft(conn: sqlite3.Connection, draft_ids: list[int]) -> dict[int, list[CheckRow]]:
    out: dict[int, list[CheckRow]] = {}
    for did in draft_ids:
        out[did] = checks_for_draft(conn, did)
    return out


def insert_check(conn: sqlite3.Connection, draft_id: int, check: ClaimCheck, model: str) -> int:
    ensure_schema(conn)
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    cur = conn.execute(
        """INSERT INTO claim_checks (draft_id, claim_index, claim, verdict, source_url, quote,
                                     note, trusted, model, checked_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(draft_id, claim_index) DO UPDATE SET
             claim=excluded.claim, verdict=excluded.verdict, source_url=excluded.source_url,
             quote=excluded.quote, note=excluded.note, trusted=excluded.trusted,
             model=excluded.model, checked_at=excluded.checked_at""",
        (
            draft_id,
            check.claim_index,
            check.claim,
            check.verdict,
            check.source_url,
            check.quote,
            check.note,
            int(check.trusted),
            model,
            now,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def delete_checks(conn: sqlite3.Connection, draft_id: int) -> None:
    ensure_schema(conn)
    conn.execute("DELETE FROM claim_checks WHERE draft_id = ?", (draft_id,))
    conn.commit()


def _norm_claim(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().rstrip(".").casefold()


def carry_over_checks(conn: sqlite3.Connection, draft_id: int, new_claims: list[str]) -> int:
    """After a revision, keep the SUPPORTED verdicts whose claim survives unchanged (same
    text up to case, spacing and a trailing full stop), re-indexed to the claim's new
    position; drop every other check. Contradicted and unverified verdicts are always
    dropped: they were handed to the drafter to fix, so the claim must be checked again.
    Returns the number of verdicts kept."""
    ensure_schema(conn)
    old = checks_for_draft(conn, draft_id)
    supported = {_norm_claim(c.claim): c for c in old if c.verdict == SUPPORTED}
    conn.execute("DELETE FROM claim_checks WHERE draft_id = ?", (draft_id,))
    kept = 0
    for i, claim in enumerate(new_claims):
        c = supported.get(_norm_claim(claim))
        if c is None:
            continue
        conn.execute(
            """INSERT INTO claim_checks (draft_id, claim_index, claim, verdict, source_url,
                                         quote, note, trusted, model, checked_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                draft_id,
                i,
                claim,
                c.verdict,
                c.source_url,
                c.quote,
                c.note,
                int(c.trusted),
                c.model,
                c.checked_at,
            ),
        )
        kept += 1
    conn.commit()
    return kept


# --- table cells ---------------------------------------------------------------------


@dataclass
class TableCheckRow:
    id: int
    draft_id: int
    row: int
    col: int
    cell: str
    verdict: str
    source_url: str
    quote: str
    note: str
    trusted: bool
    model: str
    checked_at: str

    @property
    def label(self) -> str:
        if self.verdict == SUPPORTED:
            return "supported" if self.trusted else "supported (untrusted source)"
        if self.verdict == CONTRADICTED:
            return "contradicted" if self.trusted else "contradicted (untrusted source)"
        return "unverified"

    @property
    def shown(self) -> bool:
        """Whether the cell keeps its text in the picture."""
        return self.verdict == SUPPORTED and self.trusted


def table_checks_for_draft(conn: sqlite3.Connection, draft_id: int) -> list[TableCheckRow]:
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT * FROM table_checks WHERE draft_id = ? ORDER BY row, col", (draft_id,)
    ).fetchall()
    return [
        TableCheckRow(
            id=r["id"],
            draft_id=r["draft_id"],
            row=r["row"],
            col=r["col"],
            cell=r["cell"],
            verdict=r["verdict"],
            source_url=r["source_url"],
            quote=r["quote"],
            note=r["note"],
            trusted=bool(r["trusted"]),
            model=r["model"],
            checked_at=r["checked_at"],
        )
        for r in rows
    ]


def insert_table_check(
    conn: sqlite3.Connection,
    draft_id: int,
    row: int,
    col: int,
    cell: str,
    check: ClaimCheck,
    model: str,
    checked_at: str | None = None,
) -> None:
    ensure_schema(conn)
    now = checked_at or datetime.now(UTC).replace(microsecond=0).isoformat()
    conn.execute(
        """INSERT INTO table_checks (draft_id, row, col, cell, verdict, source_url, quote,
                                     note, trusted, model, checked_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(draft_id, row, col) DO UPDATE SET
             cell=excluded.cell, verdict=excluded.verdict, source_url=excluded.source_url,
             quote=excluded.quote, note=excluded.note, trusted=excluded.trusted,
             model=excluded.model, checked_at=excluded.checked_at""",
        (
            draft_id,
            row,
            col,
            cell,
            check.verdict,
            check.source_url,
            check.quote,
            check.note,
            int(check.trusted),
            model,
            now,
        ),
    )
    conn.commit()


def delete_table_checks(conn: sqlite3.Connection, draft_id: int) -> None:
    ensure_schema(conn)
    conn.execute("DELETE FROM table_checks WHERE draft_id = ?", (draft_id,))
    conn.commit()


def carry_over_table_checks(conn: sqlite3.Connection, draft_id: int, old_table, table) -> int:
    """After a revision from `old_table` to `table`: keep every cell verdict whose row
    label, column and cell text are unchanged, at the cell's new position; drop the rest.
    Any verdict is kept, not only supported ones, because a table cell is never handed to
    the drafter to fix. Returns the number kept. `table` None (the revision has no table)
    drops them all."""
    ensure_schema(conn)
    old = table_checks_for_draft(conn, draft_id)
    conn.execute("DELETE FROM table_checks WHERE draft_id = ?", (draft_id,))
    conn.commit()
    if table is None or old_table is None or not old:
        return 0
    keyed = {}
    for c in old:
        if c.row >= len(old_table.rows):
            continue
        keyed[(_norm_claim(old_table.rows[c.row][0]), c.col, _norm_claim(c.cell))] = c
    kept = 0
    for r, c, cell in table.cells():
        prev = keyed.get((_norm_claim(table.rows[r][0]), c, _norm_claim(cell)))
        if prev is None:
            continue
        insert_table_check(
            conn,
            draft_id,
            r,
            c,
            cell,
            ClaimCheck(0, cell, prev.verdict, prev.source_url, prev.quote, prev.note, prev.trusted),
            prev.model,
            prev.checked_at,
        )
        kept += 1
    return kept


def pending_drafts_with_tables(conn: sqlite3.Connection) -> list[queue_store.DraftRow]:
    """Pending drafts whose table has not been rendered yet, oldest first."""
    rows = queue_store.list_drafts(conn, queue_store.STATUS_PENDING)
    return [r for r in rows if r.draft.table is not None and not r.image_path]


def has_contradiction(conn: sqlite3.Connection, draft_id: int) -> bool:
    return any(c.verdict == CONTRADICTED for c in checks_for_draft(conn, draft_id))


def pending_drafts_with_claims(conn: sqlite3.Connection) -> list[queue_store.DraftRow]:
    """Pending drafts that have at least one claim to verify, oldest first."""
    rows = queue_store.list_drafts(conn, queue_store.STATUS_PENDING)
    return [r for r in rows if r.draft.claims_to_verify]


def unchecked_indexes(conn: sqlite3.Connection, draft: queue_store.DraftRow) -> list[int]:
    done = {c.claim_index for c in checks_for_draft(conn, draft.id)}
    return [i for i in range(len(draft.draft.claims_to_verify)) if i not in done]
