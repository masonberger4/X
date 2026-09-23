import json
import random

import pytest

from draft.chart import Style
from swarm import mutate
from swarm.genome import DEFAULT_GENOME, SEED_DESIGNERS, Genome, Slot

G = Genome(name="p", slots=list(DEFAULT_GENOME.slots), fan_out=6, layers=2, id=1)


def child(**over):
    d = {
        "name": "c",
        "change": "x",
        "fan_out": 6,
        "layers": 2,
        "slots": [s.__dict__ for s in G.slots],
    }
    d.update(over)
    return d


def test_one_change_of_each_kind_is_accepted():
    assert mutate.validate_child(child(fan_out=8), G, set()).fan_out == 8
    assert mutate.validate_child(child(layers=3), G, set()).layers == 3
    slots = [s.__dict__ for s in G.slots]
    slots[2] = {"name": "thesis", "rule": "A different rule."}
    c = mutate.validate_child(child(slots=slots), G, set())
    assert c.slots[2].rule == "A different rule." and c.parent_id == 1 and c.notes == "x"
    # a split needs room under the six-slot cap: split a five-slot parent
    five = Genome(name="f", slots=G.slots[:4] + G.slots[-1:], fan_out=6, layers=2, id=2)
    split = [s.__dict__ for s in five.slots]
    split[2:3] = [{"name": "thesis", "rule": "a"}, {"name": "comparison", "rule": "b"}]
    assert len(mutate.validate_child(child(slots=split), five, set()).slots) == 6
    with pytest.raises(mutate.ChildError, match="outside 3-6"):
        seven = [s.__dict__ for s in G.slots]
        seven[2:3] = [{"name": "thesis", "rule": "a"}, {"name": "comparison", "rule": "b"}]
        mutate.validate_child(child(slots=seven), G, set())
    merged = [s.__dict__ for s in G.slots]
    merged[1:3] = [{"name": "science", "rule": "both"}]
    assert len(mutate.validate_child(child(slots=merged), G, set()).slots) == 5


def test_rejections():
    with pytest.raises(mutate.ChildError, match="identical"):
        mutate.validate_child(child(), G, set())
    with pytest.raises(mutate.ChildError, match="2 things"):
        mutate.validate_child(child(fan_out=8, layers=3), G, set())
    with pytest.raises(mutate.ChildError, match="taken"):
        mutate.validate_child(child(fan_out=8), G, {"c"})
    with pytest.raises(mutate.ChildError, match="outside"):
        mutate.validate_child(child(fan_out=99), G, set())
    slots = [s.__dict__ for s in G.slots]
    slots[0], slots[-1] = slots[-1], slots[0]
    with pytest.raises(mutate.ChildError, match="first slot"):
        mutate.validate_child(child(slots=slots), G, set())
    two = [s.__dict__ for s in G.slots]
    two[1] = {"name": "mechanism", "rule": "new"}
    two[4] = {"name": "risk", "rule": "new"}
    with pytest.raises(mutate.ChildError, match="2 things"):
        mutate.validate_child(child(slots=two), G, set())


def test_breed_writer_retries_on_an_invalid_answer_and_shows_parents():
    answers = iter(["garbage", json.dumps(child(layers=1))])
    prompts = []

    def call(system, user, model):
        prompts.append(user)
        return next(answers)

    parent = mutate.Parent(G, 1.4, 6, [(2.1, ["one", "two", "three"])])
    c = mutate.breed_writer([parent], parent, set(), model="m", call=call)
    assert c.layers == 1 and len(prompts) == 2
    assert "score 1.40 over 6 posts" in prompts[0] and "best post (relative 2.10)" in prompts[0]
    assert "REJECTED" in prompts[1]


def test_breed_writer_gives_up_after_attempts():
    parent = mutate.Parent(G, None, 0, [])
    with pytest.raises((mutate.ChildError, ValueError)):
        mutate.breed_writer([parent], parent, set(), model="m", call=lambda *a: "{}", attempts=2)


def test_breed_designer_steps_exactly_one_knob_inside_its_range():
    for seed in range(20):
        parent = SEED_DESIGNERS[2]
        c = mutate.breed_designer(parent, {"bold-track"}, random.Random(seed))
        before, after = Style().apply(parent.style).to_dict(), Style().apply(c.style).to_dict()
        changed = [k for k in before if before[k] != after[k]]
        assert len(changed) == 1, changed
        k = changed[0]
        if k in Style.RANGES:
            lo, hi = Style.RANGES[k]
            assert lo <= after[k] <= hi
        assert c.parent_id == parent.id and c.name != "bold-track" and c.name.startswith("bold-")
        assert Style().apply(c.style) == Style().apply(after)  # the diff round-trips


def test_slot_names_are_normalised():
    slots = [s.__dict__ for s in G.slots]
    slots[2] = {"name": " The Thesis ", "rule": "  spaced   out  "}
    c = mutate.validate_child(child(slots=slots), G, set())
    assert c.slots[2] == Slot("the_thesis", "spaced out")


def test_breed_designer_swaps_palettes_and_flips_multi_colour():
    from draft.chart import PALETTES

    parent = SEED_DESIGNERS[0]  # the house style
    seen: set[str] = set()
    for seed in range(60):
        c = mutate.breed_designer(parent, set(), random.Random(seed))
        before, after = Style().apply(parent.style).to_dict(), Style().apply(c.style).to_dict()
        changed = [k for k in before if before[k] != after[k]]
        assert len(changed) == 1, changed
        seen.add(changed[0])
        if changed[0] == "palette":
            assert after["palette"] in PALETTES and after["palette"] != "navy"
    assert {"palette", "multi_colour"} <= seen


def test_a_child_may_not_ask_for_a_url():
    slots = [s.__dict__ for s in G.slots]
    slots[-1] = {"name": "closer", "rule": "End on the source URL verbatim."}
    with pytest.raises(mutate.ChildError, match="URL"):
        mutate.validate_child(child(slots=slots), G, set())


def test_the_breeder_is_told_the_real_kpi_and_that_only_the_head_counts():
    system = mutate.mutation_system("conversation")
    assert "impressions" not in system and "replies x3" in system
    assert "FIRST post" in system
    assert "median likes" in mutate.mutation_system("likes")
