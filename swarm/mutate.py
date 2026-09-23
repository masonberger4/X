"""Phase three: breeding. A writer child is written by ONE strong-model call that reads the
top genomes and their best posts and varies exactly one thing; code checks that it did. A
designer child needs no model: one Style knob is stepped at random inside its range, a flag
flipped, or the palette swapped for another one.

`call` defaults to draft.drafter.call_anthropic, the same single network call every other
swarm step uses; tests pass a fake."""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from typing import Any

from draft.chart import Style
from draft.drafter import CallFn, call_anthropic, parse_json_response
from draft.schema import ANCHOR_WORDS, MAX_VISUALS, SHAPES, THREAD_MAX
from feedback.models import CONVERSATION, CONVERSATION_WEIGHTS
from swarm.genome import (
    CLOSER,
    HOOK,
    MAX_FAN_OUT,
    MAX_LAYERS,
    MAX_SLOTS,
    MIN_FAN_OUT,
    MIN_LAYERS,
    MIN_SLOTS,
    Designer,
    FormatGenome,
    Genome,
    Slot,
)

MAX_POST_CHARS_SHOWN = 300


@dataclass
class Parent:
    genome: Genome
    median_relative: float | None
    n: int
    threads: list[tuple[float, list[str]]]  # (relative, thread) best first


def kpi_description(kpi: str) -> str:
    """What the score measures, in words, for the breeder."""
    if kpi == CONVERSATION:
        parts = ", ".join(f"{name} x{w:g}" for name, w in CONVERSATION_WEIGHTS.items())
        return f"weighted conversation ({parts})"
    return kpi


def mutation_system(kpi: str = CONVERSATION) -> str:
    """The breeder's system prompt, naming the KPI the genomes are actually selected on."""
    return (
        "You breed writing recipes for an X account on the business and investing side of "
        "immuno-oncology biotech, written as a hedge-fund immuno-oncology analyst. A recipe "
        "(genome) is a list of SLOTS, one per post of a thread, each with a one-line rule "
        "for the cheap model that writes that post, plus fan_out (how many candidates per "
        "slot) and layers (1 = candidates only; 2+ = rounds where writers see and improve "
        "each other's candidates). You are shown the current best genomes with their scores "
        f"(median {kpi_description(kpi)} of the thread's FIRST post relative to the "
        "account's trailing median; 1.0 is average) and their best posts. Only the first "
        "post is measured, and it is the only one X shows people who do not follow the "
        "account, so the hook slot matters most. Write ONE child of the parent named in the "
        "request that varies EXACTLY ONE thing: reword one slot's rule, or split one slot "
        "into two, or merge two adjacent slots, or change fan_out, or change layers. "
        "Everything else stays identical. Never add a rule that asks for advice, price "
        "targets, hype, links or numbers not in the source; the hard rules are enforced in "
        "code and a child that fights them just loses. "
        'Answer with JSON only: {"name": "<short-kebab-name>", "change": "<one line>", '
        '"fan_out": <int>, "layers": <int>, "slots": [{"name": "...", "rule": '
        '"..."}, ...]}.'
    )


MUTATION_SYSTEM = mutation_system()

# A slot rule may never ask for a link: rule 2 bans them, so every candidate would be
# discarded (or, if it complied, the rule would be dead text the judges score against).
# A rule that FORBIDS one ("never a URL", "no link or URL") is fine. Only the word URL
# counts: "link" is also a verb the voice rules use ("link cause to consequence").
_URL_WORD = re.compile(r"\burls?\b", re.IGNORECASE)
_NEGATED_URL = re.compile(
    r"\b(?:no|never|not|without|nor|don't|do not)\W+(?:[\w'-]+\W+){0,4}?urls?\b",
    re.IGNORECASE,
)


def asks_for_url(rule: str) -> bool:
    """True when a slot rule asks for a link: a literal http(s) address, or the word URL
    not negated within a few words before it ("never a URL", "no link or URL" pass)."""
    if re.search(r"https?://", rule, re.IGNORECASE):
        return True
    return len(_URL_WORD.findall(rule)) > len(_NEGATED_URL.findall(rule))


def _fmt_genome(p: Parent) -> list[str]:
    g = p.genome
    score = f"{p.median_relative:.2f}" if p.median_relative is not None else "unscored"
    lines = [
        f"GENOME {g.name}: score {score} over {p.n} posts; fan_out {g.fan_out}, layers {g.layers}"
    ]
    for s in g.slots:
        lines.append(f"  - {s.name}: {s.rule}")
    for rel, thread in p.threads[:2]:
        lines.append(f"  best post (relative {rel:.2f}):")
        for post in thread:
            lines.append(f"      {post[:MAX_POST_CHARS_SHOWN]}")
    return lines


def mutation_prompt(parents: list[Parent], target: Parent, taken_names: set[str]) -> str:
    lines: list[str] = ["CURRENT POPULATION (best first):", ""]
    for p in parents:
        lines += _fmt_genome(p)
        lines.append("")
    lines += [
        f"Write one child of GENOME {target.genome.name}. Vary exactly one thing. Names "
        f"already taken: {', '.join(sorted(taken_names)) or 'none'}. JSON only.",
    ]
    return "\n".join(lines)


class ChildError(ValueError):
    """The model's child is not a valid one-step variation of its parent."""


def _slots_from(data: Any) -> list[Slot]:
    if not isinstance(data, list) or not data:
        raise ChildError("slots must be a non-empty list")
    slots = []
    for i, s in enumerate(data):
        if not isinstance(s, dict):
            raise ChildError(f"slots[{i}] must be an object")
        name = str(s.get("name", "")).strip().lower().replace(" ", "_")
        rule = " ".join(str(s.get("rule", "")).split())
        if not name or not rule:
            raise ChildError(f"slots[{i}] needs a name and a rule")
        slots.append(Slot(name, rule))
    return slots


def diff_count(parent: Genome, child: Genome) -> int:
    """How many things changed between parent and child, counting a slot list edit (one
    reword, one split, one merge) as one."""
    n = 0
    if child.fan_out != parent.fan_out:
        n += 1
    if child.layers != parent.layers:
        n += 1
    ps = [(s.name, s.rule) for s in parent.slots]
    cs = [(s.name, s.rule) for s in child.slots]
    if ps != cs:
        # one edit means the lists agree except for a window of 1 parent slot vs 1-2 child
        # slots (reword or split) or 2 parent slots vs 1 child slot (merge)
        i = 0
        while i < min(len(ps), len(cs)) and ps[i] == cs[i]:
            i += 1
        j = 0
        while j < min(len(ps), len(cs)) - i and ps[len(ps) - 1 - j] == cs[len(cs) - 1 - j]:
            j += 1
        p_mid, c_mid = len(ps) - i - j, len(cs) - i - j
        if (p_mid, c_mid) in ((1, 1), (1, 2), (2, 1)):
            n += 1
        else:
            n += 2  # more than one slot edit
    return n


def validate_child(data: Any, parent: Genome, taken_names: set[str]) -> Genome:
    if not isinstance(data, dict):
        raise ChildError("child must be a JSON object")
    name = str(data.get("name", "")).strip().lower().replace(" ", "-")
    if not name or name in taken_names:
        raise ChildError(f"name missing or taken: {name!r}")
    try:
        fan_out = int(data.get("fan_out", parent.fan_out))
        layers = int(data.get("layers", parent.layers))
    except (TypeError, ValueError) as exc:
        raise ChildError("fan_out and layers must be integers") from exc
    if not MIN_FAN_OUT <= fan_out <= MAX_FAN_OUT:
        raise ChildError(f"fan_out {fan_out} outside {MIN_FAN_OUT}-{MAX_FAN_OUT}")
    if not MIN_LAYERS <= layers <= MAX_LAYERS:
        raise ChildError(f"layers {layers} outside {MIN_LAYERS}-{MAX_LAYERS}")
    slots = _slots_from(data.get("slots", [s.__dict__ for s in parent.slots]))
    if not MIN_SLOTS <= len(slots) <= MAX_SLOTS:
        raise ChildError(f"{len(slots)} slots outside {MIN_SLOTS}-{MAX_SLOTS}")
    if slots[0].name != HOOK or slots[-1].name != CLOSER:
        raise ChildError(f"first slot must be {HOOK!r} and last {CLOSER!r}")
    if len({s.name for s in slots}) != len(slots):
        raise ChildError("slot names must be unique")
    inherited = {(s.name, s.rule) for s in parent.slots}
    if linked := [
        s.name for s in slots if (s.name, s.rule) not in inherited and asks_for_url(s.rule)
    ]:
        raise ChildError(f"slot rule asks for a URL, which no post may carry: {linked}")
    child = Genome(
        name=name,
        slots=slots,
        fan_out=fan_out,
        layers=layers,
        parent_id=parent.id,
        notes=" ".join(str(data.get("change", "")).split())[:300],
    )
    n = diff_count(parent, child)
    if n == 0:
        raise ChildError("child is identical to its parent")
    if n > 1:
        raise ChildError(f"child changes {n} things; exactly one is allowed")
    return child


def breed_writer(
    parents: list[Parent],
    target: Parent,
    taken_names: set[str],
    *,
    model: str,
    call: CallFn = call_anthropic,
    attempts: int = 3,
    kpi: str = CONVERSATION,
) -> Genome:
    """One strong-model call (retried on an invalid child) -> a validated child Genome.
    `kpi` is what the scores shown to the model measure (evolve.kpi)."""
    user = mutation_prompt(parents, target, taken_names)
    system = mutation_system(kpi)
    last: Exception | None = None
    for _ in range(attempts):
        raw = call(system, user, model)
        try:
            return validate_child(parse_json_response(raw), target.genome, taken_names)
        except (ChildError, ValueError, json.JSONDecodeError) as exc:
            last = exc
            user = (
                mutation_prompt(parents, target, taken_names)
                + f"\n\nYOUR PREVIOUS ANSWER WAS REJECTED: {exc}. Answer again, JSON only."
            )
    assert last is not None
    raise last


# Designer breeding is pure code: step one knob.
_STEP = {
    "font_scale": 0.1,
    "title_scale": 0.1,
    "bar_height": 0.08,
    "row_pitch": 0.015,
    "label_wrap": 6,
    "table_row_height": 0.015,
}
_FLAGS = ("highlight_first", "gridlines", "track", "multi_colour")


def breed_designer(parent: Designer, taken_names: set[str], rng: random.Random) -> Designer:
    """A child that differs from its parent in exactly one Style knob, stepped up or down
    within Style.RANGES, one flag flipped, or one choice (the palette) swapped. Colour
    knobs are drawn as often as the layout ones together, so palettes drift quickly."""
    base = Style().apply(parent.style)
    knobs = list(_STEP) + list(_FLAGS)
    colour = ["palette", "multi_colour"]
    rng.shuffle(knobs)
    if rng.random() < 0.5:
        knobs = colour + knobs
    for knob in knobs:
        current = getattr(base, knob)
        if knob in Style.CHOICES:
            options = [c for c in Style.CHOICES[knob] if c != current]
            if not options:
                continue
            new: Any = rng.choice(options)
        elif knob in _FLAGS:
            new = not current
        else:
            lo, hi = Style.RANGES[knob]
            direction = rng.choice((-1, 1))
            new = current + direction * _STEP[knob]
            if not lo <= new <= hi:
                new = current - direction * _STEP[knob]
            if knob == "label_wrap":
                new = int(round(new))
            else:
                new = round(new, 3)
            if not lo <= new <= hi or new == current:
                continue
        style = {**base.to_dict(), knob: new}
        # store only what differs from the house style so the row stays readable
        house = Style().to_dict()
        diff = {k: v for k, v in style.items() if v != house[k]}
        n = 1
        name = f"{parent.name}-{knob.replace('_', '')}"
        while name in taken_names:
            n += 1
            name = f"{parent.name}-{knob.replace('_', '')}{n}"
        return Designer(
            name=name,
            style=diff,
            parent_id=parent.id,
            notes=f"{knob}: {current} -> {new}",
        )
    raise ChildError("no knob could be stepped")


def format_neighbours(parent: FormatGenome) -> list[tuple[FormatGenome, str]]:
    """Every format one step from the parent: another shape, one more or fewer visual, one
    anchor moved, min or max posts stepped (threads). Each with a one-line note."""
    out: list[tuple[FormatGenome, str]] = []

    def child(**over: Any) -> FormatGenome:
        d = {
            "shape": parent.shape,
            "min_posts": parent.min_posts,
            "max_posts": parent.max_posts,
            "visuals": parent.visuals,
            "anchors": list(parent.anchors),
        }
        d.update(over)
        return FormatGenome(name="", parent_id=parent.id, **d)

    for shape in SHAPES:
        if shape == parent.shape:
            continue
        if shape == "thread":
            c = child(shape=shape, min_posts=3, max_posts=6, anchors=["first"] * parent.visuals)
        else:
            c = child(shape=shape, min_posts=1, max_posts=1, anchors=["first"] * parent.visuals)
        out.append((c, f"shape {parent.shape} -> {shape}"))
    if parent.visuals < MAX_VISUALS:
        out.append(
            (
                child(visuals=parent.visuals + 1, anchors=list(parent.anchors) + ["first"]),
                f"visuals {parent.visuals} -> {parent.visuals + 1}",
            )
        )
    if parent.visuals > 0:
        out.append(
            (
                child(visuals=parent.visuals - 1, anchors=list(parent.anchors)[:-1]),
                f"visuals {parent.visuals} -> {parent.visuals - 1}",
            )
        )
    if parent.shape == "thread":
        for i, a in enumerate(parent.anchors):
            for word in ANCHOR_WORDS:
                if word == a:
                    continue
                anchors = list(parent.anchors)
                anchors[i] = word
                out.append((child(anchors=anchors), f"anchor {i + 1} {a} -> {word}"))
        for lo, hi in (
            (parent.min_posts - 1, parent.max_posts),
            (parent.min_posts + 1, parent.max_posts),
            (parent.min_posts, parent.max_posts - 1),
            (parent.min_posts, parent.max_posts + 1),
        ):
            if 2 <= lo <= hi <= THREAD_MAX and (lo, hi) != (parent.min_posts, parent.max_posts):
                out.append((child(min_posts=lo, max_posts=hi), f"posts {lo}-{hi}"))
    return out


def format_name(f: FormatGenome) -> str:
    """A readable name: shape, post range, visual count and anchors."""
    posts = f"{f.min_posts}-{f.max_posts}" if f.shape == "thread" else "1"
    where = "".join(a[0] for a in f.anchors) if f.anchors else "0"
    return f"{f.shape}-{posts}-{f.visuals}{where}"


def breed_format(parent: FormatGenome, taken_names: set[str], rng: random.Random) -> FormatGenome:
    """A child one step from the parent (pure code), with an unused name."""
    options = format_neighbours(parent)
    rng.shuffle(options)
    for c, note in options:
        base = format_name(c)
        name, n = base, 1
        while name in taken_names:
            n += 1
            name = f"{base}-{n}"
        c.name = name
        c.notes = note
        return c
    raise ChildError("no neighbouring format")
