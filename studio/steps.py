"""The steps of a piece the app runs itself, each a Claude Code call of its own on the model
and effort studio/config.yaml gives it (`steps:`): the research questions of the writer's
plan (gather), and the cold fact-check's claim list and claim checks.

The writer's own session (studio/session.py) keeps the judgement: it plans, writes the
fact base from the answers, writes the post and settles what the checks flag. Every call
here is a fresh session with no standing instructions (everything it needs is in its
prompt, studio/prompt.py), run from its step's folder under the piece (STEPS_DIR), with
only the tools its step allows, a turn cap and a time limit, so its cost and time are the
app's to choose, not the writer's. Several run side by side (`steps.parallel`). Each call
is a studio_runs row with its model, effort and cost, and what it returned is kept in the
piece's folder (steps/<step>/<key>.json), so a wrong fact in a draft can be traced to the
step and the model that brought it in.

The database is only touched from the calling thread; the calls themselves run in a pool.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import claude_cli
from studio import prompt as P
from studio import store as S

log = logging.getLogger(__name__)

MAX_QUESTIONS = 30
_SAFE_KEY = re.compile(r"[^A-Za-z0-9_-]+")
VERDICTS = ("confirmed", "wrong", "unconfirmed")
UNCHECKED = "unchecked"


@dataclass
class Call:
    """One call the app makes: which step, a key unique within it (a question id, a batch),
    and its prompt."""

    step: str
    key: str
    prompt: str


@dataclass
class Answer:
    call: Call
    ok: bool
    data: dict[str, Any] | None  # the JSON object the call answered with
    text: str
    detail: str


def step_dir(workspace: Path, step: str) -> Path:
    return workspace / P.STEPS_DIR / step


def safe_key(value: Any, fallback: str) -> str:
    key = _SAFE_KEY.sub("_", str(value or "")).strip("_")[:40]
    return key or fallback


def json_object(text: str) -> dict[str, Any] | None:
    """The JSON object a call answered with: the whole text, or the stretch from its first
    '{' to its last '}' (a call may wrap it in a code fence or a sentence)."""
    text = (text or "").strip()
    for candidate in (text, text[text.find("{") : text.rfind("}") + 1]):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _log(workspace: Path, line: str) -> None:
    try:
        with open(workspace / "session.log", "a", encoding="utf-8") as fh:
            fh.write(line.rstrip() + "\n")
    except OSError:
        pass


# ---- running calls ---------------------------------------------------------------------


def run_calls(
    ctx: Any, piece: S.Piece, settings: dict[str, Any], calls: list[Call]
) -> list[Answer]:
    """Run `calls` (each on its step's settings in `settings`, the piece's recorded step
    settings), up to `parallel` at once, and record each as a studio_runs row. A call that
    cannot start or fails comes back not ok; the others still run."""
    if not calls:
        return []
    workspace = Path(piece.workspace)
    launch = ctx.launch or claude_cli.run_session
    parallel = max(1, int(settings.get("parallel") or 1))
    runs: dict[int, int] = {}
    for i, call in enumerate(calls):
        own = settings[call.step]
        runs[i] = S.start_run(
            ctx.conn, piece.id, call.step, model=own["model"], effort=own["effort"] or ""
        )

    def one(call: Call) -> claude_cli.SessionResult | Exception:
        own = settings[call.step]
        folder = step_dir(workspace, call.step)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return exc
        tag = f"[piece {piece.id} {call.step} {call.key}]"

        def on_event(event: dict[str, Any]) -> None:
            if event.get("type") == "assistant":
                for line in claude_cli.describe_event(event):
                    if line.startswith("["):  # tool uses only: the text is its answer
                        _log(workspace, f"{tag} {line}")

        minutes = float(own.get("timeout_minutes") or 0)
        try:
            return launch(
                call.prompt,
                model=own["model"],
                cwd=folder,
                session_id=str(uuid.uuid4()),
                resume=False,
                system="",
                effort=own["effort"] or None,
                tools=list(own.get("tools") or []),
                allowed=list(own.get("tools") or []),
                flags=ctx.cfg["cli_flags"],
                max_turns=int(own.get("max_turns") or 0) or None,
                timeout=minutes * 60 if minutes > 0 else None,
                transcript=folder / f"{call.key}.ndjson",
                on_event=on_event,
                cfg=ctx.root_cfg,
            )
        except Exception as exc:  # one call's failure is that call's, not the step's
            return exc

    _log(workspace, f"=== {calls[0].step}: {len(calls)} call(s), {parallel} at a time ===")
    if parallel == 1 or len(calls) == 1:
        results = [one(c) for c in calls]
    else:
        with ThreadPoolExecutor(max_workers=min(parallel, len(calls))) as pool:
            results = list(pool.map(one, calls))
    answers: list[Answer] = []
    for i, (call, res) in enumerate(zip(calls, results, strict=True)):
        if isinstance(res, Exception):
            detail = f"could not run: {res}"
            S.finish_run(ctx.conn, runs[i], outcome="error", detail=detail)
            answers.append(Answer(call, ok=False, data=None, text="", detail=detail))
            continue
        data = json_object(res.text) if res.ok else None
        ok = res.ok and data is not None
        detail = res.detail if not res.ok else ("finished" if ok else "the answer was not JSON")
        S.finish_run(
            ctx.conn,
            runs[i],
            outcome="ok" if ok else (res.subtype or "error"),
            detail=detail,
            turns=res.num_turns,
            cost_usd=float(res.cost_usd or 0.0),  # a fresh session: its total is its own
            duration_ms=res.duration_ms,
        )
        answers.append(Answer(call, ok=ok, data=data, text=res.text or "", detail=detail))
        _log(workspace, f"[piece {piece.id} {call.step} {call.key}] {'ok' if ok else detail}")
    return answers


# ---- research: the plan and the gather -------------------------------------------------


def read_plan(workspace: Path) -> list[dict[str, str]]:
    """The writer's questions from research_plan.json, cleaned: a known kind (else lookup),
    a non-empty question, a unique file-safe id; at most MAX_QUESTIONS. [] when the file
    is missing or holds none."""
    data = _read_json(workspace / P.PLAN_FILE)
    raw = data.get("questions") if isinstance(data, dict) else None
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for i, q in enumerate(raw if isinstance(raw, list) else [], 1):
        if not isinstance(q, dict) or not str(q.get("question") or "").strip():
            continue
        key = safe_key(q.get("id"), f"q{i:02d}")
        while key in seen:
            key += "_b"
        seen.add(key)
        kind = str(q.get("kind") or "").strip().lower()
        out.append(
            {
                "id": key,
                "kind": kind if kind in P.QUESTION_KINDS else "lookup",
                "question": str(q["question"]).strip(),
                "where": str(q.get("where") or "").strip(),
                "why": str(q.get("why") or "").strip(),
            }
        )
        if len(out) >= MAX_QUESTIONS:
            break
    return out


def gather_step(kind: str) -> str:
    return "gather_deep" if kind in P.DEEP_KINDS else "gather_light"


def answer_file(workspace: Path, qid: str) -> Path:
    return step_dir(workspace, "gather") / f"{qid}.json"


def gather(
    ctx: Any,
    piece: S.Piece,
    settings: dict[str, Any],
    questions: list[dict[str, str]],
    *,
    today: str,
    topic: str,
) -> tuple[int, list[str]]:
    """Put every question without an answer yet to its gather step (a resumed piece asks
    only what is still open) and write the answers, one file each and all of them in
    GATHER_FILE. Returns how many came back and the ids of those that did not."""
    workspace = Path(piece.workspace)
    open_qs = [q for q in questions if not _answered(workspace, q["id"])]
    calls = [
        Call(gather_step(q["kind"]), q["id"], P.gather_prompt(q, today=today, topic=topic))
        for q in open_qs
    ]
    by_id = {q["id"]: q for q in open_qs}
    for a in run_calls(ctx, piece, settings, calls):
        own = settings[a.call.step]
        _write_json(
            answer_file(workspace, a.call.key),
            {
                "question": by_id[a.call.key],
                "step": a.call.step,
                "model": own["model"],
                "effort": own["effort"],
                "ok": a.ok,
                "answer": a.data,
                "raw": "" if a.ok else a.text[:20000],
                "detail": a.detail,
            },
        )
    missing = [q["id"] for q in questions if not _answered(workspace, q["id"])]
    write_gather_file(workspace, questions)
    return len(questions) - len(missing), missing


def _answered(workspace: Path, qid: str) -> bool:
    data = _read_json(answer_file(workspace, qid))
    return isinstance(data, dict) and bool(data.get("ok"))


def write_gather_file(workspace: Path, questions: list[dict[str, str]]) -> None:
    """Every answer, as the writer reads them: the question, who answered (model), the
    answer, each fact with its quote and source, what was not found."""
    parts = [
        "# Gathered answers\n\nOne section per question of research_plan.json. Each was "
        "answered by another model with web access; every fact carries the quote and the "
        "URL it came from. These are leads: open the source of anything the piece leans on.\n"
    ]
    for q in questions:
        data = _read_json(answer_file(workspace, q["id"]))
        head = f"\n## {q['id']} ({q['kind']}): {q['question']}\n"
        if not isinstance(data, dict) or not data.get("ok"):
            why = data.get("detail") if isinstance(data, dict) else "not asked"
            parts.append(head + f"\nNO ANSWER ({why}).\n")
            continue
        ans = data.get("answer") or {}
        body = [head, f"*Answered by {data.get('model')}*\n", str(ans.get("answer") or "")]
        for f in ans.get("facts") or []:
            if not isinstance(f, dict):
                continue
            seen = "opened" if f.get("opened") else "snippet"
            body.append(
                f"- {f.get('fact', '')}\n  > {f.get('quote', '')}\n"
                f"  {f.get('source', '')}, {f.get('source_date', 'unknown')}: "
                f"{f.get('url', '')} ({seen})"
            )
        for miss in ans.get("not_found") or []:
            body.append(f"- NOT FOUND: {miss}")
        for sus in ans.get("suspicious") or []:
            body.append(f"- SUSPICIOUS SOURCE: {sus}")
        parts.append("\n".join(body) + "\n")
    try:
        (workspace / P.GATHER_FILE).write_text("".join(parts), encoding="utf-8")
    except OSError as exc:
        log.warning("could not write %s: %s", P.GATHER_FILE, exc)


# ---- the cold fact-check: the claim list and the claim checks ---------------------------


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("style", "script", "head", "title"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("style", "script", "head", "title") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self.parts.append(" ".join(data.split()))


def visible_text(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    return "\n".join(parser.parts)


def _piece_json(workspace: Path) -> dict[str, Any]:
    data = _read_json(workspace / P.PIECE_FILE)
    return data if isinstance(data, dict) else {}


def post_texts(workspace: Path) -> list[str]:
    """The posts in order: the files piece.json lists, else posts/*.txt."""
    listed = _piece_json(workspace).get("posts")
    names = [str(n) for n in listed] if isinstance(listed, list) else []
    paths = [workspace / n for n in names] or sorted((workspace / P.POSTS_DIR).glob("*.txt"))
    out = []
    for path in paths:
        try:
            out.append(path.read_text(encoding="utf-8"))
        except OSError:
            continue
    return out


def card_texts(workspace: Path) -> list[tuple[str, str]]:
    """Each card's file name and visible text: the cards piece.json lists, else cards/*.html."""
    listed = _piece_json(workspace).get("cards")
    names = (
        [str(c.get("file") or "") for c in listed if isinstance(c, dict)]
        if isinstance(listed, list)
        else []
    )
    paths = [workspace / n for n in names if n] or sorted((workspace / P.CARDS_DIR).glob("*.html"))
    out = []
    for path in paths:
        try:
            out.append((path.name, visible_text(path.read_text(encoding="utf-8"))))
        except OSError:
            continue
    return out


def claims_file(workspace: Path) -> Path:
    return step_dir(workspace, "claim_list") / "claims.json"


def checks_file(workspace: Path) -> Path:
    return step_dir(workspace, "claim_check") / "results.json"


def claim_list(ctx: Any, piece: S.Piece, settings: dict[str, Any]) -> tuple[list[dict], str]:
    """Every checkable claim of the finished posts and cards, listed by the claim_list step.
    Returns (claims, "") or ([], why it failed)."""
    workspace = Path(piece.workspace)
    prompt = P.claim_list_prompt(post_texts(workspace), card_texts(workspace))
    [a] = run_calls(ctx, piece, settings, [Call("claim_list", "claims", prompt)])
    raw = a.data.get("claims") if a.data else None
    if not a.ok or not isinstance(raw, list):
        return [], a.detail if not a.ok else "the claim list held no claims"
    claims = []
    for i, c in enumerate(raw, 1):
        if isinstance(c, dict) and str(c.get("claim") or "").strip():
            claims.append(
                {
                    "id": i,
                    "where": str(c.get("where") or ""),
                    "claim": str(c["claim"]).strip(),
                    "kind": str(c.get("kind") or ""),
                }
            )
    _write_json(claims_file(workspace), {"claims": claims})
    return claims, ""


def load_claims(workspace: Path) -> list[dict]:
    data = _read_json(claims_file(workspace))
    claims = data.get("claims") if isinstance(data, dict) else None
    return [c for c in claims if isinstance(c, dict)] if isinstance(claims, list) else []


def claim_check(
    ctx: Any, piece: S.Piece, settings: dict[str, Any], claims: list[dict], *, today: str
) -> list[dict]:
    """Check the claims in batches of `claim_check.batch`, side by side. Every claim comes
    back with a verdict (confirmed, wrong, unconfirmed; `unchecked` when its batch failed),
    saved in steps/claim_check/ and written out for the writer in CHECKS_FILE."""
    workspace = Path(piece.workspace)
    size = max(1, int(settings["claim_check"].get("batch") or 10))
    batches = [claims[i : i + size] for i in range(0, len(claims), size)]
    calls = [
        Call("claim_check", f"batch_{n:02d}", P.claim_check_prompt(b, today=today))
        for n, b in enumerate(batches, 1)
    ]
    verdicts: dict[int, dict[str, Any]] = {}
    for a, batch in zip(run_calls(ctx, piece, settings, calls), batches, strict=True):
        _write_json(
            step_dir(workspace, "claim_check") / f"{a.call.key}.json",
            {"claims": batch, "ok": a.ok, "answer": a.data, "detail": a.detail},
        )
        results = (a.data or {}).get("results") if a.ok else None
        for r in results if isinstance(results, list) else []:
            if not isinstance(r, dict):
                continue
            try:
                cid = int(r.get("id"))
            except (TypeError, ValueError):
                continue
            verdict = str(r.get("verdict") or "").strip().lower()
            verdicts[cid] = {**r, "verdict": verdict if verdict in VERDICTS else "unconfirmed"}
    merged = []
    for c in claims:
        r = verdicts.get(int(c["id"]), {"verdict": UNCHECKED})
        merged.append({**c, **{k: v for k, v in r.items() if k != "id"}})
    _write_json(checks_file(workspace), {"results": merged})
    write_checks_file(workspace, merged)
    return merged


def load_checks(workspace: Path) -> list[dict]:
    data = _read_json(checks_file(workspace))
    res = data.get("results") if isinstance(data, dict) else None
    return [r for r in res if isinstance(r, dict)] if isinstance(res, list) else []


def write_checks_file(workspace: Path, results: list[dict]) -> None:
    lines = [
        "# Claim checks\n",
        "Every claim the claim list found in the posts and cards, and what the checkers said.\n",
        "| # | Where | Claim | Verdict | Source |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        cells = [
            str(r.get("id")),
            str(r.get("where") or ""),
            str(r.get("claim") or ""),
            str(r.get("verdict") or ""),
            str(r.get("source") or ""),
        ]
        lines.append(
            "| " + " | ".join(c.replace("|", "/").replace("\n", " ") for c in cells) + " |"
        )
    try:
        (workspace / P.CHECKS_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        log.warning("could not write %s: %s", P.CHECKS_FILE, exc)


def flagged(results: list[dict]) -> list[dict]:
    return [r for r in results if r.get("verdict") != "confirmed"]


def write_clean_factcheck(workspace: Path, results: list[dict]) -> None:
    """factcheck.md when the checkers confirmed every claim: nothing for the writer to
    settle, so the app writes the log itself."""
    text = (
        "# Cold fact-check\n\nThe app's checkers listed "
        f"{len(results)} claims in the posts and cards and confirmed every one against "
        f"sources on the web; nothing was flagged. Every result is in {P.CHECKS_FILE}.\n"
    )
    (workspace / P.FACTCHECK_FILE).write_text(text, encoding="utf-8")
