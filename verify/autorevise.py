"""The verify-revise loop (step 2b, `run_verify.py --auto-revise`).

After the claim pass, a pending draft with a contradicted or unverified claim is sent back
through the drafter exactly as the queue's Revise button with an empty box does
(`draft/drafter.py:revise_item` with the claim problems and no instructions), stored with
`approval_queue.store.revise` under AUTO_NOTE, its supported verdicts carried over, and the
new or changed claims checked again; and so on until every claim is supported or a round
limit is hit. Rounds are bounded twice: `auto_revise.max_rounds` per run and
`auto_revise.max_rounds_per_draft` over the draft's life (0 = uncapped), counted from its `revise`
decisions carrying AUTO_NOTE. A revision that keeps the claim set unchanged is discarded
and ends the loop (the drafter changed nothing the checker could re-judge; the verdicts
stay).

This is the one place step 2b changes a draft's text, and it goes through the same path
a human's Revise takes: same schema check, same hard rules, same decision log. A table's
contradicted cells (`cell_problems`) go to the drafter with the claims; blanked cells do
not (a blank is an acceptable outcome, the table pass handles them).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from approval_queue import images
from approval_queue import store as queue_store
from draft import drafter
from draft.prompt import ClaimProblem
from verify import store, tables
from verify.verifier import CONTRADICTED, SUPPORTED

log = logging.getLogger("verify.autorevise")

AUTO_NOTE = "auto: fix fact-check failures"

DEFAULTS = {"enabled": False, "max_rounds": 3, "max_rounds_per_draft": 6}


def claim_problems(checks) -> list[ClaimProblem]:
    """The step 2b verdicts a revision must fix: contradicted claims, plus unverified ones
    the model is told to soften or drop. Supported claims are left alone."""
    out: list[ClaimProblem] = []
    for c in checks:
        if c.verdict == SUPPORTED:
            continue
        out.append(
            ClaimProblem(
                claim=c.claim,
                verdict=c.verdict,
                note=c.note,
                quote=c.quote,
                source_url=c.source_url,
            )
        )
    return out


def cell_problems(table, checks) -> list[ClaimProblem]:
    """The contradicted cells of a draft's table, worded as the fact-checker saw them
    (`tables.cell_claim`), for a revision to fix. Blanked (unverified or untrusted) cells
    are left out: a blank is fine, a contradiction is not. Empty without a table."""
    if table is None:
        return []
    out: list[ClaimProblem] = []
    for c in checks:
        if c.verdict != CONTRADICTED or c.row >= len(table.rows) or c.col >= len(table.columns):
            continue
        out.append(
            ClaimProblem(
                claim=tables.cell_claim(table, c.row, c.col),
                verdict=c.verdict,
                note=c.note,
                quote=c.quote,
                source_url=c.source_url,
            )
        )
    return out


def auto_rounds_used(conn, draft_id: int) -> int:
    """How many automatic revisions this draft has had, over every run."""
    return sum(
        1
        for x in queue_store.list_decisions(conn, draft_id)
        if x["action"] == queue_store.ACTION_REVISE and x["note"] == AUTO_NOTE
    )


@dataclass
class RoundResult:
    revised: bool
    problems: int
    kept: int = 0
    reason: str = ""  # why no revision happened, or why the loop should stop


def revise_round(conn, draft_id: int, *, lifetime_cap: int) -> RoundResult:
    """One round: if the draft has claim problems and rounds left, revise it through the
    drafter and carry the supported verdicts over. Never raises on a drafter failure: the
    draft is left as it was and the result says why."""
    row = queue_store.get_draft(conn, draft_id)
    if row is None or row.status != queue_store.STATUS_PENDING:
        return RoundResult(False, 0, reason="draft is no longer pending")
    problems = claim_problems(store.checks_for_draft(conn, draft_id))
    cells = cell_problems(row.draft.table, store.table_checks_for_draft(conn, draft_id))
    total = len(problems) + len(cells)
    if not total:
        return RoundResult(False, 0, reason="every checked claim and cell is supported")
    used = auto_rounds_used(conn, draft_id)
    if lifetime_cap > 0 and used >= lifetime_cap:  # 0 = no lifetime cap
        return RoundResult(False, total, reason=f"gave up: {used} automatic revision(s) already")
    try:
        result = drafter.revise_item(
            current=row.draft,
            instructions=None,
            claim_problems=problems,
            cell_problems=cells,
            title=row.title,
            abstract=row.abstract,
            url=row.url,
            source=row.source,
            suggested_angle=row.suggested_angle or None,
            rationale=row.rationale or None,
            call=drafter.call_anthropic,
        )
    except drafter.DraftRejected as exc:
        return RoundResult(False, total, reason=f"revision broke a hard rule: {exc}")
    except Exception as exc:  # API / network errors: keep the draft as it was
        return RoundResult(False, total, reason=f"revision failed: {exc}")
    old_claims = {store._norm_claim(c.claim) for c in row.draft.claims_to_verify}
    new_claims = {store._norm_claim(c.claim) for c in result.draft.claims_to_verify}
    if new_claims == old_claims and _table_key(row.draft.table) == _table_key(result.draft.table):
        # Nothing the checker could re-judge: the problems would come straight back. Keep
        # the draft and its verdicts as they are so the queue shows what is wrong.
        return RoundResult(False, total, reason="the drafter kept every claim and cell as it was")
    queue_store.revise(conn, draft_id, draft=result.draft, model=result.model, note=AUTO_NOTE)
    kept = store.carry_over_checks(conn, draft_id, [c.claim for c in result.draft.claims_to_verify])
    store.carry_over_table_checks(conn, draft_id, row.draft.table, result.draft.table)
    images.attach_chart(conn, draft_id, result.draft.chart, source_url=row.url)
    if result.draft.extra_visuals:
        images.attach_extra_charts(conn, draft_id, result.draft.extra_visuals, source_url=row.url)
    return RoundResult(True, total, kept=kept)


def _table_key(table):
    """What a table's verdicts hang on: its rows, normalised like the carry-over does."""
    if table is None:
        return None
    return tuple(tuple(store._norm_claim(c) for c in r) for r in table.rows)
