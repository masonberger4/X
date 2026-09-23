"""The panel's /swarm page and the ops adapters it reads through."""

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from ops import store as ops_store
from panel import app as panel_app
from panel import views
from swarm import store as swarm_store
from swarm.genome import Designer, Genome

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _seed(conn):
    swarm_store.ensure_tables(conn)
    swarm_store.seed_default(conn)
    writers = swarm_store.live_genomes(conn)
    designers = swarm_store.live_genomes(conn, "designer")
    child = Genome(
        name="kid", slots=list(writers[0].slots), fan_out=9, layers=2, parent_id=writers[0].id
    )
    swarm_store.insert_genome(conn, child)
    dchild = Designer("house-x", {"track": False}, parent_id=designers[0].id)
    swarm_store.insert_genome(conn, dchild)
    swarm_store.retire_genome(conn, writers[1].id, "lost")
    for i, (w, rel) in enumerate([(writers[0].id, 1.5), (writers[0].id, 0.5), (child.id, 2.0)]):
        run = swarm_store.record_run(
            conn,
            item_id=f"i{i}",
            cluster_id=None,
            genome_id=w,
            winner="swarm" if i < 2 else "control",
            calls=1,
            log=None,
            draft_id=i + 1,
            designer_id=designers[0].id,
        )
        swarm_store.upsert_fitness(
            conn,
            run_id=run,
            draft_id=i + 1,
            genome_id=w,
            winner="swarm" if i < 2 else "control",
            tweet_id=str(i),
            posted_at="2026-05-01T00:00:00+00:00",
            kpi="impressions",
            value=100,
            baseline=100,
            relative=rel,
            designer_id=designers[0].id,
        )
    return writers, designers, child


def test_adapters_are_empty_on_a_bare_db(conn):
    assert ops_store.fetch_swarm_population(conn) == []
    assert ops_store.fetch_swarm_bet(conn) == {}


def test_population_adapter_joins_fitness_by_kind(conn):
    writers, designers, child = _seed(conn)
    conn.row_factory = __import__("sqlite3").Row
    pop = {p["name"]: p for p in ops_store.fetch_swarm_population(conn)}
    assert pop["default-6"]["posts"] == 2 and pop["default-6"]["median_relative"] == 1.0
    assert pop["kid"]["parent_name"] == "default-6" and pop["kid"]["fan_out"] == 9
    assert pop["house"]["kind"] == "designer" and pop["house"]["posts"] == 3
    assert pop["house"]["median_relative"] == 1.5
    assert pop["wide-6"]["retired_at"] is not None and pop["wide-6"]["retired_reason"] == "lost"
    assert pop["house-x"]["style"] == {"track": False} and pop["house-x"]["parent_name"] == "house"
    bet = ops_store.fetch_swarm_bet(conn)
    assert bet["runs"] == 3 and bet["swarm_wins"] == 2 and bet["control_wins"] == 1
    assert bet["swarm_posts"] == 2 and bet["swarm_median"] == 1.0
    assert bet["control_posts"] == 1 and bet["control_median"] == 2.0


def test_swarm_rows_and_bet_row_are_pure():
    population = [
        {
            "id": 1,
            "name": "a",
            "kind": "writer",
            "parent_id": None,
            "parent_name": None,
            "created_at": NOW,
            "retired_at": None,
            "retired_reason": None,
            "notes": "",
            "posts": 3,
            "median_relative": 1.25,
            "fan_out": 6,
            "layers": 2,
            "slots": ["hook", "closer"],
            "style": {},
        },
        {
            "id": 2,
            "name": "b",
            "kind": "writer",
            "parent_id": 1,
            "parent_name": "a",
            "created_at": NOW,
            "retired_at": NOW,
            "retired_reason": "lost",
            "notes": "x",
            "posts": 0,
            "median_relative": None,
            "fan_out": 8,
            "layers": 1,
            "slots": [],
            "style": {},
        },
        {
            "id": 3,
            "name": "c",
            "kind": "writer",
            "parent_id": 2,
            "parent_name": "b",
            "created_at": NOW,
            "retired_at": None,
            "retired_reason": None,
            "notes": "",
            "posts": 0,
            "median_relative": None,
            "fan_out": 8,
            "layers": 1,
            "slots": [],
            "style": {},
        },
        {
            "id": 4,
            "name": "d",
            "kind": "designer",
            "parent_id": None,
            "parent_name": None,
            "created_at": NOW,
            "retired_at": None,
            "retired_reason": None,
            "notes": "",
            "posts": 1,
            "median_relative": 0.4,
            "style": {"track": False},
        },
    ]
    rows = views.swarm_rows(population, NOW)
    assert [r["name"] for r in rows["writer"]] == ["a", "c", "b"]  # live first, then by id
    assert [r["depth"] for r in rows["writer"]] == [0, 2, 1]
    a = rows["writer"][0]
    assert a["score"] == "1.25" and a["score_class"] == "high" and a["parent"] == "seed"
    assert a["summary"] == "fan-out 6, layers 2; hook · closer"
    assert rows["writer"][2]["state"] == "retired" and rows["writer"][2]["retired_reason"] == "lost"
    d = rows["designer"][0]
    assert d["summary"] == "track False" and d["score_class"] == "meta"
    assert views.bet_summary_row({}) == {}
    b = views.bet_summary_row({"swarm_median": 1.2, "control_median": 0.9, "runs": 1})
    assert b["verdict"] == "swarm ahead" and b["swarm_median_fmt"] == "1.20"
    assert views.bet_summary_row({"swarm_median": None, "control_median": 0.9})["verdict"] == ""


def test_swarm_page_renders_and_is_linked(db_file, conn):
    client = TestClient(panel_app.app, follow_redirects=False)
    body = client.get("/swarm").text
    assert "No swarm runs yet" in body
    _seed(conn)
    body = client.get("/swarm").text
    assert "Writer genomes" in body and "Designers" in body
    assert "<strong>kid</strong>" in body and "retired" in body and "swarm wins" in body
    assert 'href="/swarm"' in client.get("/").text
    for path in ("/swarm", "/runs", "/feedback"):
        assert "--live" not in client.get(path).text


def test_formats_appear_in_the_population_and_on_the_page(db_file, conn):
    _seed(conn)
    pop = {p["name"]: p for p in ops_store.fetch_swarm_population(conn)}
    assert pop["long-1"]["kind"] == "format" and pop["long-1"]["shape"] == "long"
    assert pop["thread-2-ends"]["anchors"] == ["first", "last"]
    rows = views.swarm_rows(list(pop.values()), NOW)
    by_name = {r["name"]: r for r in rows["format"]}
    assert by_name["thread-2-ends"]["summary"] == "thread, 3-6 posts, 2 pictures on first, last"
    assert by_name["single-1"]["summary"] == "single, one post, 1 picture on first"
    assert by_name["thread-0"]["summary"] == "thread, 3-6 posts, 0 pictures"
    client = TestClient(panel_app.app, follow_redirects=False)
    body = client.get("/swarm").text
    assert "Formats" in body and "<strong>long-1</strong>" in body


def test_format_fitness_is_counted_on_the_page(db_file, conn):
    """A format is credited with every post drawn in it, so the population shows its score
    beside the writers' and designers' (it used to show 0 posts whatever had run)."""
    swarm_store.ensure_tables(conn)
    swarm_store.seed_default(conn)
    fmt = {f.name: f.id for f in swarm_store.live_genomes(conn, "format")}["single-1"]
    for i, rel in enumerate([0.5, 1.5, 2.5]):
        run = swarm_store.record_run(
            conn,
            item_id=f"f{i}",
            cluster_id=None,
            genome_id=None,
            winner="control",
            calls=1,
            log=None,
            draft_id=10 + i,
            format_id=fmt,
        )
        swarm_store.upsert_fitness(
            conn,
            run_id=run,
            draft_id=10 + i,
            genome_id=None,
            winner="control",
            tweet_id=f"f{i}",
            posted_at=f"2026-05-0{i + 1}T00:00:00+00:00",
            kpi="conversation",
            value=rel,
            baseline=1.0,
            relative=rel,
            format_id=fmt,
        )
    pop = {p["name"]: p for p in ops_store.fetch_swarm_population(conn)}
    assert pop["single-1"]["posts"] == 3 and pop["single-1"]["median_relative"] == 1.5
    assert pop["thread-0"]["posts"] == 0
