"""The engine with a fake model that answers by prompt shape. No network."""

import json
import random

import pytest

from draft.drafter import check_hard_rules
from swarm import engine
from swarm.genome import DEFAULT_GENOME
from swarm.prompts import JUDGE_SYSTEM, THREAD_JUDGE_SYSTEM, Brief
from tests.conftest import ABSTRACT, URL

BRIEF = Brief(title="Title t1", abstract=ABSTRACT, url=URL, source="pubmed")
CFG = {
    "model": "cheap",
    "assembler_model": "",
    "fan_out": 3,
    "layers": 2,
    "judge_votes": 3,
    "max_similarity": 0.95,
    "control": {"enabled": True},
}


class FakeModel:
    """Answers cell prompts with a distinct post per call, judges with B, and the assembly
    with the cells it was handed."""

    def __init__(self, bad_every=0):
        self.calls = []
        self.n = 0
        self.bad_every = bad_every

    def __call__(self, system, user, model):
        self.calls.append((system, user, model))
        self.n += 1
        if system == JUDGE_SYSTEM:
            return json.dumps({"winner": "B", "reason": "second"})
        if system == THREAD_JUDGE_SYSTEM:
            return json.dumps({"winner": "A", "reason": "first"})
        if "Assemble the draft JSON" in user:
            cells = [
                line.split("] ", 1)[1]
                for line in user.splitlines()
                if line.startswith("[") and "] " in line
            ]
            return json.dumps(
                {
                    "thread": cells,
                    "suggested_visual": "v",
                    "why_it_matters": "w",
                    "claims_to_verify": [],
                    "chart": {
                        "title": "Outcomes",
                        "labels": ["ORR", "PFS"],
                        "values": [88, 14.6],
                        "unit": "",
                    },
                }
            )
        slot = next(line for line in user.splitlines() if line.startswith("YOUR SLOT: "))
        slot = slot.split(": ", 1)[1]
        layer = "syn" if "CANDIDATES FROM THE PREVIOUS ROUND" in user else "prop"
        if self.bad_every and self.n % self.bad_every == 0:
            return f"{slot} {layer} claims ORR 99% which is invented"
        tail = f" {URL}" if slot == "closer" else ""
        # the counter is spelled with letters: a digit would fail the verbatim-number rule
        tag = "".join(chr(ord("a") + int(d)) for d in str(self.n))
        return f"{slot} {layer} post {tag} reads the 88% ORR well{tail}"


def test_run_swarm_builds_one_cell_per_slot_and_passes_hard_rules():
    fake = FakeModel()
    res = engine.run_swarm(BRIEF, DEFAULT_GENOME, CFG, call=fake, sleep=lambda s: None)
    names = DEFAULT_GENOME.slot_names()
    assert list(res.cells) == names
    assert res.draft_result.draft.thread == [res.cells[n] for n in names]
    assert check_hard_rules(res.draft_result.draft, url=URL, source="pubmed") == []
    assert res.draft_result.model == "swarm:cheap"
    # per slot: fan_out proposals + fan_out synthesis = 6 cell calls, plus judge calls
    cell_calls = [c for c in fake.calls if c[0] not in (JUDGE_SYSTEM, THREAD_JUDGE_SYSTEM)]
    assert len(cell_calls) == 6 * len(names) + 1  # + assembly
    syn = [c for c in cell_calls if "CANDIDATES FROM THE PREVIOUS ROUND" in c[1]]
    assert len(syn) == 3 * len(names)
    # every synthesis prompt saw the three layer-1 proposals
    assert all(f"3. {n}" in c[1] or "3. " in c[1] for c, n in zip(syn, names, strict=False))
    # the closer prompt carried the URL rule; the second slot saw the first slot's pick
    closer_prompt = next(c[1] for c in cell_calls if "YOUR SLOT: closer" in c[1])
    assert f"contain the URL exactly as given: {URL}" in closer_prompt
    mech_prompt = next(c[1] for c in cell_calls if "YOUR SLOT: mechanism" in c[1])
    assert f"[hook] {res.cells['hook']}" in mech_prompt
    assert res.calls == len(fake.calls)
    assert any("assembly" in row for row in res.log)


def test_cells_with_invented_numbers_are_dropped_before_the_tournament():
    fake = FakeModel(bad_every=2)
    res = engine.run_swarm(BRIEF, DEFAULT_GENOME, CFG, call=fake, sleep=lambda s: None)
    assert all("99%" not in cell for cell in res.cells.values())
    dropped = [r for r in res.log if r.get("problems")]
    assert dropped and all("99%" in r["problems"][0] for r in dropped)


def test_slot_with_no_survivor_raises_swarm_failed():
    fake = FakeModel(bad_every=1)
    with pytest.raises(engine.SwarmFailed, match="hook"):
        engine.run_swarm(BRIEF, DEFAULT_GENOME, CFG, call=fake, sleep=lambda s: None)


def test_assembly_hard_rule_failure_is_swarm_failed():
    class NoUrl(FakeModel):
        def __call__(self, system, user, model):
            out = super().__call__(system, user, model)
            if "Assemble the draft JSON" in user:
                d = json.loads(out)
                d["thread"] = ["a", "b", "c"]  # drops the URL
                return json.dumps(d)
            return out

    with pytest.raises(engine.SwarmFailed, match="assembly failed"):
        engine.run_swarm(BRIEF, DEFAULT_GENOME, CFG, call=NoUrl(), sleep=lambda s: None)


def test_compare_majority_with_randomised_order_and_tie_to_control():
    from draft.schema import Draft

    swarm = Draft(["s1", "s2", "s3"], "", "")
    control = Draft(["c1", "c2", "c3"], "", "")
    orders = []

    def judge_for_swarm(system, user, model):
        a_is_swarm = "THREAD A:\n  1. s1" in user
        orders.append(a_is_swarm)
        return json.dumps({"winner": "A" if a_is_swarm else "B"})

    v = engine.compare(swarm, control, BRIEF, CFG, call=judge_for_swarm, rng=random.Random(1))
    assert v.winner == "swarm" and len(v.votes) == 3 and v.calls == 3
    assert all(vote["pick"] == "swarm" for vote in v.votes)
    v = engine.compare(swarm, control, BRIEF, {**CFG, "judge_votes": 2}, call=lambda *a: "?")
    assert v.winner == "control"
    # over many votes both presentation orders occur
    engine.compare(swarm, control, BRIEF, {**CFG, "judge_votes": 20}, call=judge_for_swarm)
    assert True in orders and False in orders
