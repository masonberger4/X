"""Phase three: breeding. A writer child is written by ONE strong-model call that reads the
top genomes and their best posts and varies exactly one thing; code checks that it did. A
designer child needs no model: one Style knob is stepped at random inside its range.

`call` defaults to draft.drafter.call_anthropic, the same single network call every other
swarm step uses; tests pass a fake."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from typing import Any

from draft.chart import Style
from draft.drafter import CallFn, call_anthropic, parse_json_response
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


MUTATION_SYSTEM = (
    "You breed writing recipes for an X account on the business and investing side of "
    "immuno-oncology biotech, written as a hedge-fund immuno-oncology analyst. A recipe "
    "(genome) is a list of SLOTS, one per post of a thread, each with a one-line rule for "
    "the cheap model that writes that post, plus fan_out (how many candidates per slot) and "
    "layers (1 = candidates only; 2+ = rounds where writers see and improve each other's "
    "candidates). You are shown the current best genomes with their scores (median "
    "impressions relative to the account's trailing median; 1.0 is average) and their "
    "best posts. Write ONE child of the parent named in the request that varies EXACTLY ONE "
    "thing: reword one slot's rule, or split one slot into two, or merge two adjacent "
    "slots, or change fan_out, or change layers. Everything else stays identical. Never "
    "add a rule that asks for advice, price targets, hype or numbers not in the source; "
    "the hard rules are enforced in code and a child that fights them just loses. "
    'Answer with JSON only: {"name": "<short-kebab-name>", "change": "<one line>", '
    '"fan_out": <int>, "layers": <int>, "slots": [{"name": "...", "rule": '
    '"..."}, ...]}.'
)


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
) -> Genome:
    """One strong-model call (retried on an invalid child) -> a validated child Genome."""
    user = mutation_prompt(parents, target, taken_names)
    last: Exception | None = None
    for _ in range(attempts):
        raw = call(MUTATION_SYSTEM, user, model)
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
_FLAGS = ("highlight_first", "gridlines", "track")


def breed_designer(parent: Designer, taken_names: set[str], rng: random.Random) -> Designer:
    """A child that differs from its parent in exactly one Style knob, stepped up or down
    within Style.RANGES (or one flag flipped)."""
    base = Style().apply(parent.style)
    knobs = list(_STEP) + list(_FLAGS)
    rng.shuffle(knobs)
    for knob in knobs:
        current = getattr(base, knob)
        if knob in _FLAGS:
            new: Any = not current
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
