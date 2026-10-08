"""The playbook every session reads, its history, and its rewrite from what X says.

The file is the data folder's `studio_playbook.md` (studio/settings.py:playbook_path; the
shipped seed studio/playbook.md until the first save). Every version ever applied is
also a row in studio_playbook_versions, so the performance page can show what changed and
why, and put any earlier one back. Writing goes through `save`, which records the version
and replaces the file atomically (a session starting at the same moment reads either the
old playbook or the new one, never half of each).

`rewrite` is the learning loop's one model call (`call_rewriter`, through
claude_cli.run_claude with no tools): the current playbook, the evidence and the editor's
hand edits in, a new playbook and its changelog out, checked by studio/learn.py's
`parse_rewrite` before it is saved. learn.playbook `auto` applies it, `propose` keeps it for
the editor; a failed call or an unusable reply changes nothing. A rewrite that changes the
voices (the "## Voices" section, studio/voices.py) is always kept for the editor, whatever
learn.playbook says: the account's voices change only when a human applies the change.

A playbook saved before there were voices has no Voices section; `current_text` reads it
with the shipped seed's, so its sessions still get voices and its next save keeps them.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from studio import learn as L
from studio import store as S
from studio import voices as V
from studio.settings import DEFAULT_PLAYBOOK, PLAYBOOK_NAME, playbook_path

log = logging.getLogger(__name__)


def seed_text() -> str:
    """The shipped seed (studio/playbook.md), "" when it is missing."""
    try:
        return DEFAULT_PLAYBOOK.read_text(encoding="utf-8")
    except OSError:
        return ""


def current_text(data_dir: Path) -> str:
    """The playbook in use, with the seed's voices when it has no Voices section of its own
    (a copy from before voices): what the next session's brief is made from."""
    try:
        text = playbook_path(data_dir).read_text(encoding="utf-8")
    except OSError:
        return ""
    return V.with_seed(text, seed_text())


def _write(data_dir: Path, text: str) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / PLAYBOOK_NAME
    fd, tmp = tempfile.mkstemp(prefix=".playbook-", suffix=".md", dir=str(data_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def ensure_seeded(conn: sqlite3.Connection, data_dir: Path) -> None:
    """The history starts with the playbook in use when it was first asked for: the
    shipped seed, or an editor's copy made before versions were kept."""
    if S.current_playbook_version(conn) is not None:
        return
    text = current_text(data_dir)
    if not text.strip():
        return
    source = S.PLAYBOOK_SEED if text == seed_text() else S.PLAYBOOK_EDITOR
    S.add_playbook_version(conn, text=text, source=source)


def save(
    conn: sqlite3.Connection,
    data_dir: Path,
    text: str,
    *,
    source: str,
    changelog: list[str] | tuple[str, ...] = (),
    evidence: str = "",
    based_on: int | None = None,
    pieces: list[int] | tuple[int, ...] = (),
) -> int:
    """Record a version and, unless it is a proposal, make it the playbook sessions read."""
    if not text.strip():
        raise ValueError("the playbook cannot be empty")
    ensure_seeded(conn, data_dir)
    version_id = S.add_playbook_version(
        conn,
        text=text,
        source=source,
        changelog=changelog,
        evidence=evidence,
        based_on=based_on,
        pieces=pieces,
    )
    if source != S.PLAYBOOK_PROPOSAL:
        _write(data_dir, text)
    return version_id


def revert(conn: sqlite3.Connection, data_dir: Path, version_id: int) -> int:
    """Put an earlier version back, as a new version (the history is never rewritten)."""
    old = S.get_playbook_version(conn, version_id)
    if old is None:
        raise KeyError(version_id)
    return save(
        conn,
        data_dir,
        old.text,
        source=S.PLAYBOOK_REVERT,
        changelog=[f"back to version {old.id} ({old.source}, {old.created_at})"],
        based_on=old.id,
    )


def apply_proposal(conn: sqlite3.Connection, data_dir: Path, version_id: int) -> int:
    """The editor applies the open learned proposal: it is saved as a new learned version
    (so the newest applied version is always the file's text) and stops being open. A
    proposal overtaken by a later save is refused (KeyError)."""
    proposal = S.open_proposal(conn)
    if proposal is None or proposal.id != version_id:
        raise KeyError(version_id)
    new_id = save(
        conn,
        data_dir,
        proposal.text,
        source=S.PLAYBOOK_LEARNED,
        changelog=proposal.changelog,
        evidence=proposal.evidence,
        based_on=proposal.id,
        pieces=proposal.pieces,
    )
    S.mark_playbook_applied(conn, proposal.id)
    return new_id


# --- the rewrite --------------------------------------------------------------------------


def call_rewriter(
    system: str, user: str, *, model: str, effort: str, root_cfg: dict[str, Any], timeout: float
) -> str:
    """The rewrite's one CLI call: no tools, the given model and effort, and its own time
    limit in place of claude_code.timeout_seconds (a max-effort rewrite can outlast it)."""
    import claude_cli

    cli = {**(root_cfg.get("claude_code") or {}), "timeout_seconds": timeout}
    return claude_cli.run_claude(
        user,
        system=system,
        model=model,
        cfg={**root_cfg, "claude_code": cli},
        effort=effort or None,
    )


@dataclass(frozen=True)
class RewriteOutcome:
    ok: bool
    message: str
    version_id: int | None = None
    applied: bool = False


Caller = Callable[..., str]


def rewrite(
    conn: sqlite3.Connection,
    cfg: dict[str, Any],
    data_dir: Path,
    pieces: Sequence[L.Measured],
    edits: Sequence[L.Edit],
    *,
    evidence: str,
    root_cfg: dict[str, Any],
    call: Caller | None = None,
    by_voice: Sequence[L.VoiceEdits] = (),
) -> RewriteOutcome:
    """Rewrite the playbook from the evidence; save it as a learned version (applied) or a
    proposal (learn.playbook: propose, or any rewrite that changes the voices). Never raises
    for a failed call or a bad reply."""
    import claude_cli

    lcfg = cfg["learn"]
    max_words = int(lcfg["max_words"])
    ensure_seeded(conn, data_dir)  # so the history shows what this version replaced
    current = current_text(data_dir)
    system, user = L.rewrite_prompt(
        current, pieces, edits, evidence=evidence, max_words=max_words, by_voice=by_voice
    )
    timeout = float(lcfg.get("timeout_minutes") or 0) * 60 or 1800.0
    try:
        reply = (call or call_rewriter)(
            system,
            user,
            model=str(lcfg["model"]),
            effort=str(lcfg.get("effort") or ""),
            root_cfg=root_cfg,
            timeout=timeout,
        )
    except claude_cli.ClaudeCliError as exc:
        return RewriteOutcome(False, f"the playbook rewrite call failed: {exc}")
    try:
        result = L.parse_rewrite(
            reply,
            max_words=max_words,
            voices_off=V.has_section(current) and not V.parse(current),
        )
    except L.RewriteRejected as exc:
        return RewriteOutcome(False, f"the rewritten playbook was not used: {exc}")
    voices_changed = V.changed(current, result.playbook)
    apply = lcfg["playbook"] == "auto" and not voices_changed
    learned_from = [p.piece_id for p in L.scored(pieces)]
    version_id = save(
        conn,
        data_dir,
        result.playbook,
        source=S.PLAYBOOK_LEARNED if apply else S.PLAYBOOK_PROPOSAL,
        changelog=result.changelog,
        evidence=evidence,
        based_on=getattr(S.current_playbook_version(conn), "id", None),
        pieces=learned_from,
    )
    if apply:
        what = "applied: the next session reads it"
    elif lcfg["playbook"] == "auto":
        what = "proposed, since it changes the voices: apply it on the performance page"
    else:
        what = "proposed: apply it on the performance page"
    return RewriteOutcome(
        True,
        f"playbook version {version_id} learned from {len(learned_from)} scored piece(s), {what}"
        + (f" ({len(result.changelog)} change(s))" if result.changelog else ""),
        version_id=version_id,
        applied=apply,
    )
