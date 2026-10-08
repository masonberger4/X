"""The studio's learning loop, the pure part: how each posted piece did on X, what that says
about angles, shapes, hooks and cards, the lean the next piece is offered, the text every
session reads, and the prompt and checks for the playbook rewrite. No DB, no network, no
clock: the adapters in studio/store.py hand in the rows, `now` and `rng` are parameters.

A piece is scored on the head of its post (the first tweet) at `learn.horizon_hours`
after posting, on the account's KPI (`conversation` by default: replies and quotes x3,
bookmarks and reposts x2, likes x1, impressions x0.05; feedback/models.py), and compared
with the median of every post the account made in the `learn.baseline_days` before it
(studio or drafter): relative = (value + smoothing) / (baseline + smoothing). A piece
with too few posts before it to compare with is measured but not yet scored.

What the scores feed:
- `arm_stats`: per angle, shape, hook style, card count and voice, how many pieces and the
  mean log relative (shown as "x the median").
- `lean`: one Thompson draw per arm among what is on offer, so a value that has done
  better is suggested more often and one with little evidence still gets tried.
- `draw_voice`: the voice a new piece is given. The app assigns it rather than suggesting
  it, at random (never the voice of the piece before) until `voices.lean_min_measured`
  scored pieces carry one, then by the same kind of Thompson draw.
- `evidence_block`: the "WHAT X SAYS" section of the prompts, with honest sample sizes.
- `voice_edits`: per voice, what the editor did with its pieces in the queue (changed by
  hand, how much, revisions asked, rejected): a signal that comes with every piece, long
  before X has said anything.
- `rewrite_prompt` / `parse_rewrite`: one model call that rewrites the playbook from the
  evidence and the editor's hand edits, checked here before it is ever used.
"""

from __future__ import annotations

import difflib
import json
import math
import random
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median, stdev
from typing import Any

from feedback.models import CONVERSATION, CONVERSATION_WEIGHTS, METRICS, Metrics
from studio import voices as V

ARMS = ("angle", "shape", "hook_style", "cards", "voice")
ARM_LABELS = {
    "angle": "Angles",
    "shape": "Shapes",
    "hook_style": "Hook styles",
    "cards": "Cards per piece",
    "voice": "Voices",
}
# A relative of 0 has no logarithm; it counts as this (a post with nothing against a
# baseline with something, and no smoothing).
LOG_FLOOR = 0.05
SMALL_SAMPLE = 20  # below this many scored pieces the evidence is called hints
SPREAD_FLOOR = 0.2  # the least spread a draw assumes, so a few alike posts never look certain


def card_bucket(n: int) -> str:
    return "3+" if n >= 3 else str(max(0, int(n)))


def kpi_value(metrics: Mapping[str, int], kpi: str = CONVERSATION) -> float:
    """The KPI of one snapshot (`metrics` holds the six stored counts)."""
    counts = Metrics(**{m: int(metrics.get(m) or 0) for m in METRICS})
    return float(counts.get(kpi))


def describe_kpi(kpi: str = CONVERSATION) -> str:
    if kpi != CONVERSATION:
        return kpi
    parts = []
    for name, weight in CONVERSATION_WEIGHTS.items():
        parts.append(f"{name} x{weight:g}")
    return "conversation (" + ", ".join(parts) + ")"


def pick_snapshot(
    snapshots: Sequence[Mapping[str, Any]], posted_at: datetime, horizon_hours: float
) -> Mapping[str, Any] | None:
    """The snapshot a head is scored on: the earliest taken at least `horizon_hours` after
    posting, so every post is compared at about the same age (None while it is younger);
    with a horizon of 0, the newest. `snapshots` are oldest first, each with an aware
    `captured_at`. A snapshot the editor typed in (`manual`) is taken as it is."""
    if not snapshots:
        return None
    manual = [s for s in snapshots if s.get("manual")]
    if manual:
        return manual[-1]
    if horizon_hours <= 0:
        return snapshots[-1]
    for snap in snapshots:
        if (snap["captured_at"] - posted_at).total_seconds() >= horizon_hours * 3600:
            return snap
    return None


@dataclass(frozen=True)
class Head:
    """One of the account's posted heads, studio or not, with its KPI at the horizon: the
    material the baseline is made of."""

    draft_id: int
    posted_at: datetime
    value: float


@dataclass
class Measured:
    """A posted studio piece with a usable snapshot of its head."""

    piece_id: int
    draft_id: int
    posted_at: datetime
    value: float
    metrics: dict[str, int]
    title: str = ""
    opening: str = ""
    angle: str = ""
    shape: str = ""
    hook_style: str = ""
    cards: int = 0
    voice: str = ""  # the playbook voice it was written in ('' for none)
    source: str = "x"  # "x": step 4's snapshot; "manual": typed in by the editor
    url: str = ""
    baseline: float | None = None
    relative: float | None = None

    @property
    def log_rel(self) -> float | None:
        if self.relative is None:
            return None
        return math.log(max(self.relative, LOG_FLOOR))

    def arm(self, arm: str) -> str:
        if arm == "cards":
            return card_bucket(self.cards)
        return str(getattr(self, arm) or "")


def score(
    pieces: Iterable[Measured],
    heads: Iterable[Head],
    *,
    baseline_days: float,
    min_baseline_posts: int,
    smoothing: float,
) -> list[Measured]:
    """Fill `baseline` and `relative` on every piece, oldest first. The baseline is the
    median KPI of the account's heads in the `baseline_days` strictly before the piece
    (the piece's own post left out); fewer than `min_baseline_posts` of them, or a zero
    denominator, leaves the piece unscored."""
    window = timedelta(days=baseline_days)
    all_heads = list(heads)
    out = sorted(pieces, key=lambda p: (p.posted_at, p.piece_id))
    for p in out:
        earlier = [
            h.value
            for h in all_heads
            if h.draft_id != p.draft_id and p.posted_at - window <= h.posted_at < p.posted_at
        ]
        if len(earlier) < max(1, min_baseline_posts):
            p.baseline, p.relative = None, None
            continue
        base = float(median(earlier))
        p.baseline = base
        denom = base + smoothing
        p.relative = (p.value + smoothing) / denom if denom > 0 else None
    return out


@dataclass(frozen=True)
class ArmStat:
    arm: str
    value: str
    n: int
    mean_log: float

    @property
    def times(self) -> float:
        """The typical piece with this value, as a multiple of the account's median."""
        return math.exp(self.mean_log)


def scored(pieces: Iterable[Measured]) -> list[Measured]:
    return [p for p in pieces if p.log_rel is not None]


def arm_stats(pieces: Iterable[Measured], arm: str) -> list[ArmStat]:
    """Per value of one arm, the scored pieces and their mean log relative, best first."""
    groups: dict[str, list[float]] = {}
    for p in scored(pieces):
        value = p.arm(arm)
        if value:
            groups.setdefault(value, []).append(p.log_rel)  # type: ignore[arg-type]
    stats = [ArmStat(arm, v, len(xs), sum(xs) / len(xs)) for v, xs in groups.items()]
    return sorted(stats, key=lambda s: (-s.mean_log, -s.n, s.value))


def spread(pieces: Iterable[Measured], *, default: float, floor: float, min_n: int = 10) -> float:
    """The spread of one piece's log relative that Thompson sampling assumes: the measured
    one once there are `min_n` scored pieces (a small sample understates it and would make
    the draws greedy), `default` before that, never below `floor`."""
    logs = [p.log_rel for p in scored(pieces)]
    if len(logs) >= max(2, min_n):
        return max(stdev(logs), floor)  # type: ignore[arg-type]
    return max(default, floor)


def posterior(stat: ArmStat | None, *, prior_sd: float, post_sd: float) -> tuple[float, float]:
    """(mean, sd) of a value's true mean log relative: a normal prior at the account's
    median (0) with `prior_sd`, updated with its pieces, each worth a spread of `post_sd`."""
    if stat is None or not stat.n:
        return 0.0, prior_sd
    prec, data_prec = 1.0 / prior_sd**2, stat.n / post_sd**2
    mean_ = stat.mean_log * data_prec / (prec + data_prec)
    return mean_, math.sqrt(1.0 / (prec + data_prec))


def thompson(
    candidates: Sequence[str],
    stats: Sequence[ArmStat],
    *,
    rng: random.Random,
    prior_sd: float,
    post_sd: float,
) -> str:
    """One plausible true score per candidate from its posterior; the highest draw wins.
    A value is picked about as often as it is likely to be the best."""
    if not candidates:
        raise ValueError("nothing to choose from")
    by_value = {s.value: s for s in stats}
    best, best_draw = candidates[0], -math.inf
    for value in candidates:
        mean_, sd = posterior(by_value.get(value), prior_sd=prior_sd, post_sd=post_sd)
        draw = rng.gauss(mean_, sd)
        if draw > best_draw:
            best, best_draw = value, draw
    return best


@dataclass(frozen=True)
class Lean:
    angle: str  # "" when the editor chose the angle
    shape: str
    hook_style: str
    measured: int  # scored pieces the draw stood on

    def line(self) -> str:
        parts = [f"the angle {self.angle}"] if self.angle else []
        parts += [f"the shape {self.shape}", f"the hook style {self.hook_style}"]
        towards = ", ".join(parts[:-1]) + " and " + parts[-1]
        return (
            f"If the story supports it, lean towards {towards}. On this account's "
            f"{self.measured} measured pieces they are the likeliest to do best, and the draw "
            "also tries what has little evidence yet. The story comes first: choose "
            "otherwise when it is better served."
        )

    def as_dict(self) -> dict[str, str]:
        return {"angle": self.angle, "shape": self.shape, "hook_style": self.hook_style}


def lean(
    pieces: Iterable[Measured],
    *,
    angles: Sequence[str],
    shapes: Sequence[str],
    hooks: Sequence[str],
    rng: random.Random,
    prior_sd: float,
    default_sd: float,
    min_measured: int,
) -> Lean | None:
    """A Thompson draw per arm among what is on offer (the angles the variety rules leave,
    the shapes, the hooks not used lately). None until `min_measured` pieces are scored:
    with nothing to stand on, a "lean" would be a coin toss dressed up as evidence. A
    single angle on offer (the editor named it) leaves the angle out of the lean."""
    pool = scored(pieces)
    if len(pool) < max(1, min_measured) or not shapes or not hooks:
        return None
    sd = spread(pool, default=default_sd, floor=SPREAD_FLOOR)

    def draw(arm: str, options: Sequence[str]) -> str:
        return thompson(list(options), arm_stats(pool, arm), rng=rng, prior_sd=prior_sd, post_sd=sd)

    return Lean(
        # One angle on offer is the editor's choice, not something to lean towards.
        angle=draw("angle", angles) if len(angles) > 1 else "",
        shape=draw("shape", shapes),
        hook_style=draw("hook_style", hooks),
        measured=len(pool),
    )


@dataclass(frozen=True)
class VoiceDraw:
    key: str
    how: str  # "random", "evidence" (a Thompson draw) or "only" (one voice to give)
    measured: int  # scored pieces with a voice the draw stood on


def draw_voice(
    pieces: Iterable[Measured],
    voices: Sequence[str],
    *,
    previous: str,
    rng: random.Random,
    min_measured: int,
    prior_sd: float,
    default_sd: float,
) -> VoiceDraw | None:
    """The voice a new piece is written in, among `voices` (keys), never `previous` (the
    voice of the piece before it) when there is another. At random until `min_measured`
    scored pieces carry a voice (0: always at random): a few posts say little, and an even
    draw keeps the comparison fair. Then a Thompson draw on what X says, so a voice that has
    done better is given more often and one with little evidence still gets pieces. None
    when there is no voice to give."""
    keys = list(dict.fromkeys(k for k in voices if k))
    if not keys:
        return None
    if len(keys) == 1:
        return VoiceDraw(keys[0], "only", 0)
    candidates = [k for k in keys if k != previous] or keys
    pool = [p for p in scored(pieces) if p.voice]
    if min_measured > 0 and len(pool) >= min_measured:
        sd = spread(pool, default=default_sd, floor=SPREAD_FLOOR)
        stats = arm_stats(pool, "voice")
        key = thompson(candidates, stats, rng=rng, prior_sd=prior_sd, post_sd=sd)
        return VoiceDraw(key, "evidence", len(pool))
    return VoiceDraw(rng.choice(candidates), "random", len(pool))


# --- what the editor did with each voice's pieces ------------------------------------------

REVIEW_POSTED = "posted"
REVIEW_APPROVED = "approved"
REVIEW_REJECTED = "rejected"


@dataclass(frozen=True)
class Reviewed:
    """A studio piece with a voice that the editor has decided on in the queue."""

    piece_id: int
    voice: str
    outcome: str  # REVIEW_POSTED, REVIEW_APPROVED or REVIEW_REJECTED
    changed: float  # share of its words the editor's hand edits changed (0: as written)
    revisions: int  # times the studio rewrote it on the editor's request


@dataclass(frozen=True)
class VoiceEdits:
    voice: str
    decided: int  # pieces approved, posted or rejected
    edited: int  # of the ones not rejected, those the editor changed by hand
    changed: float | None  # mean share of words changed, over the ones not rejected
    revisions: int  # revisions asked, over every decided piece
    rejected: int

    @property
    def kept(self) -> int:
        return self.decided - self.rejected


def edit_share(before: str, after: str) -> float:
    """How much of a text the editor changed, by words: 0 untouched, 1 all of it (difflib's
    ratio, so a word cut and a word added count alike)."""
    a, b = before.split(), after.split()
    if not a and not b:
        return 0.0
    return 1.0 - difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def voice_edits(reviews: Iterable[Reviewed]) -> list[VoiceEdits]:
    """Per voice, what the editor did with its pieces: the voice rewritten least first."""
    groups: dict[str, list[Reviewed]] = {}
    for r in reviews:
        if r.voice:
            groups.setdefault(r.voice, []).append(r)
    out = []
    for voice, rows in groups.items():
        kept = [r for r in rows if r.outcome != REVIEW_REJECTED]
        out.append(
            VoiceEdits(
                voice=voice,
                decided=len(rows),
                edited=sum(1 for r in kept if r.changed > 0),
                changed=sum(r.changed for r in kept) / len(kept) if kept else None,
                revisions=sum(r.revisions for r in rows),
                rejected=len(rows) - len(kept),
            )
        )
    return sorted(
        out,
        key=lambda v: (
            v.rejected / v.decided,
            v.changed if v.changed is not None else 1.0,
            v.revisions / v.decided,
            -v.decided,
            v.voice,
        ),
    )


def voice_edits_line(v: VoiceEdits) -> str:
    parts = [f"{v.decided} piece{'s' if v.decided != 1 else ''} decided"]
    if v.kept:
        parts.append(
            f"{v.edited} of {v.kept} kept changed by hand, "
            f"{(v.changed or 0) * 100:.0f}% of the words on average"
        )
    parts.append(f"{v.revisions} revision{'s' if v.revisions != 1 else ''} asked")
    parts.append(f"{v.rejected} rejected")
    return f"- {v.voice}: " + "; ".join(parts)


def lean_shares(
    pieces: Iterable[Measured],
    arm: str,
    candidates: Sequence[str],
    *,
    rng: random.Random,
    prior_sd: float,
    default_sd: float,
    draws: int = 2000,
) -> dict[str, float]:
    """How often the lean suggests each candidate on the evidence so far (its chance of
    being the best one): the share of `draws` Thompson draws it wins. For the dashboard;
    a session is offered one draw."""
    if not candidates:
        return {}
    pool = scored(pieces)
    sd = spread(pool, default=default_sd, floor=SPREAD_FLOOR)
    stats = arm_stats(pool, arm)
    n = max(1, int(draws))
    wins = dict.fromkeys(candidates, 0)
    for _ in range(n):
        wins[thompson(list(candidates), stats, rng=rng, prior_sd=prior_sd, post_sd=sd)] += 1
    return {k: v / n for k, v in wins.items()}


def _quote(text: str, limit: int = 180) -> str:
    one = " ".join(text.split())
    return one if len(one) <= limit else one[: limit - 1].rstrip() + "…"


def _ranked(stats: Sequence[ArmStat], top: int = 3, bottom: int = 2) -> str:
    def fmt(s: ArmStat) -> str:
        return f"{s.value} {s.times:.1f}x ({s.n} piece{'s' if s.n != 1 else ''})"

    if len(stats) <= top + bottom:
        return "; ".join(fmt(s) for s in stats)
    return (
        "; ".join(fmt(s) for s in stats[:top])
        + "; ... ; "
        + "; ".join(fmt(s) for s in stats[-bottom:])
    )


def evidence_block(
    pieces: Sequence[Measured],
    *,
    kpi: str = CONVERSATION,
    horizon_hours: float = 48,
    baseline_days: float = 30,
    voices: bool = True,
) -> str:
    """The "WHAT X SAYS" text every session reads; "" when nothing is scored yet. A session
    gets it without the voices (`voices=False`): the app gives each piece its voice, and a
    writer told which voices do well would drift towards them and blur its own."""
    pool = scored(pieces)
    if not pool:
        return ""
    lines = [
        f"{len(pool)} studio piece{'s' if len(pool) != 1 else ''} measured on X so far, each "
        f"by its first post's {describe_kpi(kpi)} {horizon_hours:g} hours after posting, "
        f"against the median of the account's posts in the {baseline_days:g} days before it."
    ]
    if len(pool) < SMALL_SAMPLE:
        lines.append(
            "That is too few to be sure of anything: read what follows as hints, not rules. "
            "One post's numbers are mostly the story and the day."
        )
    for arm in ARMS:
        if arm == "voice" and not voices:
            continue
        stats = arm_stats(pool, arm)
        if stats:
            lines.append(f"- {ARM_LABELS[arm]}: {_ranked(stats)}.")
    best = sorted(pool, key=lambda p: -(p.relative or 0))
    for label, chosen in (("Did best", best[:2]), ("Did worst", best[::-1][:2])):
        for p in chosen:
            voice = f", {p.voice} voice" if voices and p.voice else ""
            lines.append(
                f"- {label}: {p.title or 'piece ' + str(p.piece_id)} ({p.angle}, {p.shape}, "
                f"{p.hook_style} hook, {p.cards} card{'s' if p.cards != 1 else ''}{voice}): "
                f'{p.relative:.1f}x the median. It opened: "{_quote(p.opening)}"'
            )
        if len(pool) < 3:
            break  # with one or two pieces the best and the worst are the same posts
    return "\n".join(lines)


# --- the playbook rewrite -------------------------------------------------------------------


@dataclass(frozen=True)
class Edit:
    """What the editor changed by hand in a studio draft before it went out."""

    piece_id: int
    before: str
    after: str


@dataclass(frozen=True)
class Rewrite:
    playbook: str
    changelog: list[str] = field(default_factory=list)


class RewriteRejected(ValueError):
    """The model's playbook could not be used; the current one stays."""


def unseen(pieces: Iterable[Measured], learned_from: Iterable[int]) -> list[Measured]:
    """The scored pieces the last rewrite was not given (`learned_from`: its piece ids)."""
    seen = set(learned_from)
    return [p for p in scored(pieces) if p.piece_id not in seen]


def rewrite_due(
    pieces: Sequence[Measured],
    *,
    learned_from: Iterable[int],
    last_rewrite_at: datetime | None,
    now: datetime,
    min_new: int,
    min_hours: float,
) -> bool:
    """A rewrite when `min_new` scored pieces are new to it (the last rewrite was not
    given them: a piece posted before it may only reach its horizon after it) and the last
    one is at least `min_hours` old."""
    if last_rewrite_at is not None and (now - last_rewrite_at) < timedelta(hours=min_hours):
        return False
    return len(unseen(pieces, learned_from)) >= max(1, min_new)


REWRITE_SYSTEM = """You keep the playbook for a writer: a short working note the writer reads before every piece. The writer is an AI analyst who researches and writes long-form X posts, with designed cards, on the business and investing side of immuno-oncology biotech (CAR-T and cell therapy, T-cell engagers and bispecifics, checkpoint and adjacent IO science; trials, catalysts, deals, money). A human editor approves every piece before it is posted.

Your job: rewrite the playbook so the next pieces do better on X, from the evidence you are given: how each posted piece did against the account's own median, which angles, shapes, hooks, card counts and voices did best and worst, what the editor changed by hand before posting (the editor's changes are strong evidence of taste), and, by voice, how often the editor rewrote, revised or rejected its pieces.

How:
- Keep what still holds. Change what the evidence contradicts, add what it shows, cut what no longer earns its place. Do not drop the editorial principles (sourcing, the reality check, labelled estimates, the fact-check habits) unless the evidence clearly says they cost readers.
- Small samples are weak evidence: one or two posts are a hint, say so in the changelog and word the playbook as a hypothesis to test ("try", "lean towards"), not a law.
- Write rules the writer can act on, with the numbers that justify them where they help ("catalyst maps have done 2x the median on 4 posts: when a story has dated catalysts, show them as a timeline card").
- Never relax the lines that are never crossed: no investment advice (no buy, sell or hold calls, no price targets of the account's own, an analyst's target cited only with what it rests on, no promised returns), no medical advice, no links in posts, no fabricated numbers, quotes or handles, every number sourced. Never tell the writer to do anything the editor would have to undo.
- The writer is a person, not a newswire: keep the first person and the writer's own reactions ("I couldn't believe the data", "this deal doesn't make any sense to me", "I wonder why they didn't include another dose"). Never write that out of the playbook.
- ## Voices lists the voices the app gives the pieces, one per piece at random ("### key: Name", then a few sentences on how that voice sounds, with a line or two of it). The account's numbers are kept by key: keep a voice's key whenever you keep the voice, and sharpen its wording only to make it more itself. To try a different voice, add one with a new key; to drop one the evidence has turned against, remove it. Change the voices only when the evidence by voice (X results, and what the editor changed, revised or rejected) supports it, with the numbers in the changelog: the editor reads every change to them before it is used. Keep {min_voices} to {max_voices} voices, each at most {voice_words} words and each the same person with a personality of its own, speaking in the first person.
- Plain markdown under the title "# Playbook", at most {max_words} words not counting ## Voices, with these sections in this order: ## Openings, ## Substance the editor values, ## Mistakes caught in fact-checks (do not repeat), ## Format, ## Cards, ## Voices, ## Still to learn (the feedback loop fills these in).

Reply with ONLY a JSON object: {{"playbook": "<the whole new playbook, markdown>", "changelog": ["<one line per change: what changed, and the evidence>"]}}"""

# The headings every playbook keeps (the seed's, plus Cards), matched by their start.
REQUIRED_SECTIONS = (
    "## Openings",
    "## Substance",
    "## Mistakes caught",
    "## Format",
    "## Cards",
    V.HEADING,
    "## Still to learn",
)
MAX_LEARNED_VOICES = 6


def rewrite_prompt(
    playbook: str,
    pieces: Sequence[Measured],
    edits: Sequence[Edit],
    *,
    evidence: str,
    max_words: int,
    by_voice: Sequence[VoiceEdits] = (),
) -> tuple[str, str]:
    """(system, user) for the one model call that rewrites the playbook. `by_voice`: what
    the editor did with each voice's pieces (voice_edits)."""
    system = REWRITE_SYSTEM.format(
        max_words=max_words,
        min_voices=V.MIN_LEARNED,
        max_voices=MAX_LEARNED_VOICES,
        voice_words=V.MAX_WORDS,
    )
    rows = []
    for p in sorted(scored(pieces), key=lambda q: q.posted_at):
        voice = f", voice {p.voice}" if p.voice else ""
        rows.append(
            f"- {p.posted_at:%Y-%m-%d} {p.title or 'piece ' + str(p.piece_id)}: angle "
            f"{p.angle}, {p.shape}, {p.hook_style} hook, {p.cards} card(s){voice}; "
            f"{p.relative:.2f}x the median ({p.value:g} against {p.baseline:g}). Opened: "
            f'"{_quote(p.opening, 300)}"'
        )
    edit_rows = []
    for e in edits[:12]:
        edit_rows.append(
            f'- piece {e.piece_id}\n  BEFORE: "{_quote(e.before, 400)}"\n'
            f'  AFTER: "{_quote(e.after, 400)}"'
        )
    off = (
        [
            "THE VOICES\nThe editor has turned the voices off: keep the ## Voices heading with no "
            "voice under it."
        ]
        if V.has_section(playbook) and not V.parse(playbook)
        else []
    )
    user = "\n\n".join(
        [
            "THE CURRENT PLAYBOOK\n" + (playbook.strip() or "(empty)"),
            *off,
            "WHAT X SAYS\n" + (evidence or "(nothing measured yet)"),
            "EVERY MEASURED PIECE\n" + ("\n".join(rows) or "(none)"),
            "WHAT THE EDITOR CHANGED BY HAND BEFORE POSTING (most recent first)\n"
            + ("\n".join(edit_rows) or "(no hand edits)"),
            "WHAT THE EDITOR DID WITH EACH VOICE'S PIECES (the least rewritten first)\n"
            + ("\n".join(voice_edits_line(v) for v in by_voice) or "(none decided yet)"),
            "Rewrite the playbook. Reply with the JSON object only.",
        ]
    )
    return system, user


def parse_rewrite(text: str, *, max_words: int, voices_off: bool = False) -> Rewrite:
    """The model's reply as a Rewrite, or RewriteRejected: not JSON, no playbook, a section
    missing or too long, a voice malformed, or too few or too many voices (the Voices
    section has limits of its own and is left out of the word count). (The playbook is
    never posted: what the writer then writes is checked at polish like any post, so the
    advice and link patterns are not run on it.) `voices_off`: the current playbook lists
    no voice (the editor turned them off), so the rewrite may list none either."""
    raw = text.strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise RewriteRejected("the reply is not a JSON object")
    try:
        data = json.loads(raw[start : end + 1])
    except ValueError as exc:
        raise RewriteRejected(f"the reply is not valid JSON ({exc})") from exc
    playbook = data.get("playbook") if isinstance(data, dict) else None
    if not isinstance(playbook, str) or not playbook.strip():
        raise RewriteRejected("the reply has no playbook")
    playbook = playbook.strip() + "\n"
    missing = [s for s in REQUIRED_SECTIONS if s not in playbook]
    if missing:
        raise RewriteRejected("the playbook lacks " + ", ".join(missing))
    words = len(re.findall(r"\S+", V.without(playbook)))
    if words > max_words * 1.15:
        raise RewriteRejected(f"the playbook runs {words} words (the limit is {max_words})")
    wrong = V.problems(playbook)
    if wrong:
        raise RewriteRejected("its voices: " + "; ".join(wrong))
    n = len(V.parse(playbook))
    if not (voices_off and n == 0) and not V.MIN_LEARNED <= n <= MAX_LEARNED_VOICES:
        raise RewriteRejected(
            f"it lists {n} voice(s); keep {V.MIN_LEARNED} to {MAX_LEARNED_VOICES}"
        )
    changelog = data.get("changelog")
    lines = [" ".join(str(c).split()) for c in changelog] if isinstance(changelog, list) else []
    return Rewrite(playbook=playbook, changelog=[c for c in lines if c][:20])
