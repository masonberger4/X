"""The swarm engine: builds a thread one slot at a time from many cheap calls, assembles the
step 2 JSON through the drafter's own hard-rule loop, and lets a jury compare the result
with the control draft.

Every model call goes through `call(system, user, model)`, by default
draft.drafter.call_anthropic (the one network call step 2 already has); tests pass a fake.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from draft.drafter import CallFn, DraftRejected, DraftResult, call_anthropic, generate
from draft.prompt import build_system_prompt
from draft.schema import Draft
from swarm import prompts
from swarm.cells import cell_problems, dedupe, similarity, tournament
from swarm.genome import Genome, Slot
from swarm.prompts import Brief

log = logging.getLogger(__name__)


@dataclass
class SwarmResult:
    draft_result: DraftResult
    cells: dict[str, str]
    log: list[dict] = field(default_factory=list)
    calls: int = 0
    genome_id: int | None = None


@dataclass
class Verdict:
    winner: str  # 'swarm' | 'control'
    votes: list[dict] = field(default_factory=list)
    calls: int = 0


class SwarmFailed(Exception):
    """No candidate survived for a slot, or the assembly failed every hard rule."""


def _swarm_model(cfg: dict[str, Any]) -> str:
    model = str(cfg.get("model") or "").strip()
    if not model:
        raise RuntimeError("swarm/config.yaml `model` is empty")
    return model


def _candidates_for_slot(
    brief: Brief,
    slot: Slot,
    chosen: dict[str, str],
    *,
    fan_out: int,
    layers: int,
    model: str,
    call: CallFn,
    counter: list[int],
    log_rows: list[dict],
) -> list[str]:
    """Layer 1 proposals, then `layers - 1` synthesis rounds that each see every earlier
    round (Mixture-of-Agents). Cells that fail the per-post rules are dropped as they land."""
    system = prompts.cell_system_prompt()
    all_rounds: list[str] = []
    survivors: list[str] = []
    for layer in range(1, layers + 1):
        round_out: list[str] = []
        for k in range(fan_out):
            if layer == 1:
                user = prompts.propose_prompt(brief, slot, chosen)
            else:
                user = prompts.synthesise_prompt(brief, slot, chosen, all_rounds)
            try:
                text = call(system, user, model).strip().strip('"')
            except Exception as exc:  # one failed cell never stops the slot
                log.warning("%s layer %d cell %d failed: %s", slot.name, layer, k, exc)
                continue
            counter[0] += 1
            problems = cell_problems(
                text,
                source_text=brief.source_text,
                url=brief.url,
                slot=slot.name,
                is_preprint=brief.preprint,
            )
            log_rows.append({"slot": slot.name, "layer": layer, "text": text, "problems": problems})
            if problems:
                log.debug("%s layer %d dropped: %s", slot.name, layer, problems)
                continue
            round_out.append(text)
        all_rounds.extend(round_out)
        survivors = round_out or survivors  # a fully failed synthesis round keeps the last good one
    return survivors


def run_swarm(
    brief: Brief,
    genome: Genome,
    cfg: dict[str, Any],
    *,
    call: CallFn = call_anthropic,
    examples_block: str | None = None,
    rng: random.Random | None = None,
    sleep: Callable[[float], None] | None = None,
) -> SwarmResult:
    """Build one draft with the swarm. Raises SwarmFailed when a slot has no usable
    candidate or the assembly fails every hard rule."""
    model = _swarm_model(cfg)
    assembler_model = str(cfg.get("assembler_model") or "").strip() or model
    fan_out = int(cfg.get("fan_out", genome.fan_out) or genome.fan_out)
    layers = int(cfg.get("layers", genome.layers) or genome.layers)
    max_sim = float(cfg.get("max_similarity", 0.85))
    counter = [0]
    log_rows: list[dict] = []
    chosen: dict[str, str] = {}
    judge_system = prompts.JUDGE_SYSTEM

    for slot in genome.slots:
        cands = _candidates_for_slot(
            brief,
            slot,
            chosen,
            fan_out=fan_out,
            layers=layers,
            model=model,
            call=call,
            counter=counter,
            log_rows=log_rows,
        )
        cands = dedupe(cands, max_sim)
        if not cands:
            raise SwarmFailed(f"no usable candidate for slot {slot.name!r}")

        def judge(a: str, b: str, _slot: Slot = slot) -> str:
            user = prompts.judge_prompt(brief, _slot, chosen, a, b)
            try:
                answer = call(judge_system, user, model)
            except Exception as exc:
                log.warning("judge call failed for %s: %s", _slot.name, exc)
                return a
            counter[0] += 1
            w = prompts.parse_winner(answer)
            return b if w == "B" else a

        winner, matches = tournament(cands, judge)
        log_rows.append({"slot": slot.name, "tournament": matches, "candidates": len(cands)})
        chosen[slot.name] = winner
        log.info("slot %s: %d candidates, chose %r", slot.name, len(cands), winner[:60])

    system = build_system_prompt(examples_block)
    user = prompts.assemble_prompt(brief, genome, chosen)
    kwargs: dict[str, Any] = {}
    if sleep is not None:
        kwargs["sleep"] = sleep
    try:
        result = generate(
            system,
            user,
            model=assembler_model,
            url=brief.url,
            source=brief.source,
            source_text=brief.source_text,
            call=call,
            **kwargs,
        )
    except DraftRejected as exc:
        raise SwarmFailed("assembly failed hard rules: " + "; ".join(exc.reasons)) from exc
    counter[0] += result.attempts
    drift = [
        {
            "slot": name,
            "kept": max((similarity(text, p) for p in result.draft.thread), default=0.0),
        }
        for name, text in chosen.items()
    ]
    log_rows.append({"assembly": drift, "attempts": result.attempts})
    result.model = f"swarm:{model}"
    return SwarmResult(
        draft_result=result, cells=chosen, log=log_rows, calls=counter[0], genome_id=genome.id
    )


def compare(
    swarm: Draft,
    control: Draft,
    brief: Brief,
    cfg: dict[str, Any],
    *,
    call: CallFn = call_anthropic,
    rng: random.Random | None = None,
) -> Verdict:
    """A jury of `judge_votes` thread judges, A/B order randomised per vote. Majority wins;
    a tie (or no parseable votes) goes to the control."""
    rng = rng or random.Random()
    model = _swarm_model(cfg)
    n = int(cfg.get("judge_votes", 3) or 3)
    votes: list[dict] = []
    calls = 0
    for _ in range(n):
        swarm_first = rng.random() < 0.5
        a, b = (swarm.thread, control.thread) if swarm_first else (control.thread, swarm.thread)
        try:
            user = prompts.thread_judge_prompt(brief, a, b)
            answer = call(prompts.THREAD_JUDGE_SYSTEM, user, model)
        except Exception as exc:
            log.warning("thread judge failed: %s", exc)
            continue
        calls += 1
        w = prompts.parse_winner(answer)
        if w is None:
            votes.append({"pick": None, "swarm_first": swarm_first})
            continue
        pick = "swarm" if (w == "A") == swarm_first else "control"
        votes.append({"pick": pick, "swarm_first": swarm_first})
    for_swarm = sum(v["pick"] == "swarm" for v in votes)
    for_control = sum(v["pick"] == "control" for v in votes)
    return Verdict("swarm" if for_swarm > for_control else "control", votes, calls)
