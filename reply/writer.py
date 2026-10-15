"""One reply from screenshots: the Claude call and the check-and-retry around it.

`call_replier` is the only network call here (one Claude Code CLI run through
`claude_cli.run_claude`, the screenshots attached to its private folder, the Read tool
plus the tools `replies.tools` gives, its own time limit); tests replace it. When the
editor gave the link of the account's own post the screenshots answer, `own_context` hands
the writer that post as it went out and its studio piece's fact base (an attachment, cut at
`replies.factbase_max_chars`). `write`
asks for a reply, checks it (reply/checks.py) and, when it fails, sends it back with what
is wrong, up to `replies.max_attempts` answers. A CLI failure is raised to the caller.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from reply import checks, prompt

log = logging.getLogger(__name__)

OUTCOME_REPLY = "reply"
OUTCOME_SKIP = "skip"
OUTCOME_BLOCKED = "blocked"

#: The writer always reads the screenshots; config.yaml's `replies.tools` adds to it.
READ_TOOL = "Read"
#: The file name the account's own post's fact base is attached under.
FACTBASE_NAME = "your_post_factbase.md"


def call_replier(
    system: str,
    user: str,
    *,
    model: str,
    effort: str,
    root_cfg: dict[str, Any],
    tools: list[str],
    timeout: float,
    attachments: dict[str, bytes],
) -> str:
    """The writer's one CLI call: the screenshots beside it, its own tools and time limit."""
    import claude_cli

    cli = {**(root_cfg.get("claude_code") or {}), "timeout_seconds": timeout}
    return claude_cli.run_claude(
        user,
        system=system,
        model=model,
        cfg={**root_cfg, "claude_code": cli},
        effort=effort or None,
        tools=list(tools),
        attachments=attachments,
    )


@dataclass
class Outcome:
    kind: str  # OUTCOME_REPLY | OUTCOME_SKIP | OUTCOME_BLOCKED
    author: str = ""
    post_text: str = ""
    text: str = ""
    why: str = ""
    reason: str = ""  # the skip reason
    problems: list[str] = field(default_factory=list)
    attempts: int = 0


def settings(root_cfg: dict[str, Any]) -> dict[str, Any]:
    """`replies:` with the shipped defaults, and the model from `models:`."""
    r = root_cfg.get("replies") or {}
    models = root_cfg.get("models") or {}
    tools = [READ_TOOL] + [str(t) for t in (r.get("tools") or []) if str(t) != READ_TOOL]
    return {
        "max_attempts": max(1, int(r.get("max_attempts", 2))),
        "max_chars": min(280, int(r.get("max_chars", 270))),
        "max_shots": max(1, int(r.get("max_shots", 4))),
        "max_shot_mb": float(r.get("max_shot_mb", 8)),
        "tools": tools,
        "timeout_seconds": float(r.get("timeout_seconds", 300)),
        "factbase_max_chars": max(0, int(r.get("factbase_max_chars", 30000))),
        "model": str(models.get("replier") or ""),
        "effort": str(models.get("replier_effort") or ""),
    }


def write(
    shots: list[Path],
    root_cfg: dict[str, Any],
    *,
    post_url: str = "",
    note: str = "",
    own_texts: list[str] | None = None,
    own_factbase: str = "",
    call: Callable[..., str] | None = None,
) -> Outcome:
    """Ask for a reply to the screenshots (and, with `own_texts`, as the author of that post
    of the account's: `own_factbase` is its piece's fact base, attached when given)."""
    s = settings(root_cfg)
    if not s["model"]:
        raise ValueError("models.replier is not set in config.yaml")
    if not shots:
        raise ValueError("no screenshot to read")
    call = call or call_replier
    names = [f"screenshot_{k}{p.suffix.lower()}" for k, p in enumerate(shots, 1)]
    attachments = {name: p.read_bytes() for name, p in zip(names, shots, strict=True)}
    factbase_name = ""
    if own_texts and own_factbase.strip():
        factbase_name = FACTBASE_NAME
        attachments[factbase_name] = own_factbase.encode("utf-8")
    system = prompt.system_prompt(s["max_chars"])
    author, post_text, previous, probs = "", "", "", []
    for attempt in range(1, s["max_attempts"] + 1):
        allowed = checks.known_handles(root_cfg, author)
        user = prompt.build_user(
            names,
            handles=allowed,
            post_url=post_url,
            note=note,
            previous=previous,
            problems=probs,
            own_texts=own_texts,
            own_factbase=factbase_name,
        )
        raw = call(
            system,
            user,
            model=s["model"],
            effort=s["effort"],
            root_cfg=root_cfg,
            tools=s["tools"],
            timeout=s["timeout_seconds"],
            attachments=attachments,
        )
        try:
            answer = prompt.parse_answer(raw)
        except ValueError as exc:
            previous, probs = (raw or "")[:500], [str(exc)]
            log.info("reply: attempt %d unreadable: %s", attempt, exc)
            continue
        author, post_text = answer.author or author, answer.post_text or post_text
        if answer.skip:
            return Outcome(OUTCOME_SKIP, author, post_text, reason=answer.skip, attempts=attempt)
        text = checks.clean(answer.reply, author)
        probs = checks.problems(
            text, allowed=checks.known_handles(root_cfg, author), max_chars=s["max_chars"]
        )
        if not probs:
            return Outcome(
                OUTCOME_REPLY, author, post_text, text=text, why=answer.why, attempts=attempt
            )
        previous = text
        log.info("reply: attempt %d failed the checks: %s", attempt, probs)
    return Outcome(
        OUTCOME_BLOCKED,
        author,
        post_text,
        text=previous,
        problems=probs,
        attempts=s["max_attempts"],
    )


def own_context(conn, own_url: str, max_chars: int) -> tuple[list[str], str]:
    """The account's own post behind `own_url` (its texts as they went out) and its studio
    piece's fact base, cut at `max_chars` ("" when it has none). Raises ValueError when no
    logged post of the account has that link."""
    from publish.store import tweet_id_from_url
    from reply import store
    from studio.prompt import FACTBASE_FILE

    own = store.own_post(conn, tweet_id_from_url(own_url) or "")
    if own is None:
        raise ValueError(
            "your post's link is not logged: add it to the post on the studio's performance "
            "page (or the copy-paste page) first"
        )
    factbase = ""
    if own.workspace and max_chars:
        path = Path(own.workspace) / FACTBASE_FILE
        try:
            factbase = path.read_text(encoding="utf-8")
        except OSError:
            log.info("reply: no fact base at %s", path)
        if len(factbase) > max_chars:
            factbase = factbase[:max_chars] + f"\n\n[... cut at {max_chars} characters]\n"
    return own.texts, factbase


def run_request(rid: int, root_cfg: dict[str, Any], call: Callable[..., str] | None = None) -> str:
    """Write the reply for request `rid` and record what came back (the panel runs this in a
    thread of its own). Never raises: a failure is recorded on the request."""
    from reply import store

    conn = store.connect()
    try:
        req = store.get(conn, rid)
        if req is None or req.status != store.WRITING:
            return "gone"
        s = settings(root_cfg)
        model = s["model"]
        try:
            own_texts, factbase = (
                own_context(conn, req.own_url, s["factbase_max_chars"]) if req.own_url else ([], "")
            )
            out = write(
                req.shot_paths(),
                root_cfg,
                post_url=req.post_url,
                note=req.note,
                own_texts=own_texts,
                own_factbase=factbase,
                call=call,
            )
        except Exception as exc:  # the CLI, a timeout, a missing screenshot
            log.warning("reply request %d not written: %s", rid, exc)
            store.finish(conn, rid, status=store.FAILED, error=str(exc)[:500], model=model)
            return store.FAILED
        status = {
            OUTCOME_REPLY: store.READY,
            OUTCOME_SKIP: store.SKIPPED,
            OUTCOME_BLOCKED: store.BLOCKED,
        }[out.kind]
        store.finish(
            conn,
            rid,
            status=status,
            author=out.author,
            post_text=out.post_text,
            reply=out.text,
            why=out.why or out.reason,
            problems=out.problems,
            model=model,
            attempts=out.attempts,
        )
        return status
    finally:
        conn.close()
