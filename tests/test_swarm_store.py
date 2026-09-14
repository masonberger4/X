import sqlite3

from draft.schema import Draft
from swarm import store
from swarm.genome import DEFAULT_GENOME, Genome


def test_genome_json_round_trip():
    g = Genome.from_json(DEFAULT_GENOME.to_json(), id=7)
    assert g.id == 7 and g.name == DEFAULT_GENOME.name
    assert g.slot_names() == DEFAULT_GENOME.slot_names()
    assert g.slots[0].rule == DEFAULT_GENOME.slots[0].rule


def test_seed_active_and_runs():
    conn = sqlite3.connect(":memory:")
    store.ensure_tables(conn)
    gid = store.seed_default(conn)
    assert store.seed_default(conn) == gid  # idempotent
    g = store.active_genome(conn)
    assert g.id == gid and g.name == "default-6"

    run_id = store.record_run(
        conn, item_id="i1", cluster_id=3, genome_id=gid, winner="swarm", calls=42, log={"x": 1}
    )
    store.record_variant(
        conn, run_id, role="swarm", model="swarm:cheap", draft=Draft(["a", "b", "c"], "v", "w")
    )
    store.record_variant(
        conn, run_id, role="control", model="strong", draft=None, problems=["too long"]
    )
    store.set_run_draft(conn, run_id, 99)
    runs = store.list_runs(conn)
    assert len(runs) == 1 and runs[0].draft_id == 99 and runs[0].winner == "swarm"
    variants = store.list_variants(conn, run_id)
    assert [v["role"] for v in variants] == ["swarm", "control"]
    assert variants[0]["ok"] == 1 and variants[1]["ok"] == 0
    assert variants[1]["problems"] == "too long"
