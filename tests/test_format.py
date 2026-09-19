"""Step 9 phase four: the format is a gene. Schema, hard rules, prompt text, storage,
publishing anchors, the swarm engine on a single and a long post, and format breeding."""

import json
import random

import pytest

from approval_queue import store as qstore
from draft import drafter
from draft.prompt import HARD_RULES, build_system_prompt, hard_rules
from draft.schema import (
    MAX_POST_CHARS,
    Format,
    SchemaError,
    long_format,
    single_format,
    validate_output,
)
from publish import store as pstore
from publish.thread import ThreadError, split_thread
from swarm import engine, mutate
from swarm.genome import SEED_FORMATS, FormatGenome
from swarm.prompts import SINGLE_SLOT, Brief
from tests.conftest import ABSTRACT, URL, seed_item
from tests.test_swarm_engine import CFG, DEFAULT_GENOME, FakeModel

CHART = {"title": "Outcomes", "labels": ["ORR", "PFS"], "values": [88, 14.6], "unit": ""}
CHART2 = {"title": "Cohort", "labels": ["ORR", "n"], "values": [88, 97], "unit": ""}


def out(**over):
    d = {
        "thread": ["one", "two", "three"],
        "suggested_visual": "v",
        "why_it_matters": "w",
        "claims_to_verify": [],
        "chart": CHART,
    }
    d.update(over)
    return d


# ---- Format and validate_output ------------------------------------------------------


def test_format_bounds():
    assert Format().resolve_anchors(4) == [1]
    assert Format(visuals=2, anchors=("first", "last")).resolve_anchors(4) == [1, 4]
    assert Format(visuals=2, anchors=("middle", "last")).resolve_anchors(5) == [3, 5]
    assert single_format().max_chars == MAX_POST_CHARS and long_format().max_chars == 4000
    with pytest.raises(ValueError):
        Format(shape="article")
    with pytest.raises(ValueError):
        Format(visuals=3, anchors=("first",) * 3)
    with pytest.raises(ValueError):
        Format(visuals=1, anchors=())
    with pytest.raises(ValueError):
        Format(min_posts=1)
    with pytest.raises(ValueError):
        Format(shape="long", max_chars=280)
    # a single or long shape is exactly one post: no post carries a link
    assert (single_format().min_posts, single_format().max_posts) == (1, 1)
    assert Format(shape="long", min_posts=3, max_posts=3, max_chars=2000).max_posts == 1
    assert Format.from_dict(Format(visuals=0, anchors=()).to_dict()) == Format(
        visuals=0, anchors=()
    )
    assert Format.from_dict(None) is None


def test_validate_output_without_a_format_is_the_old_physics():
    d = validate_output(out())
    assert d.shape == "thread" and d.anchors == [1] and d.max_chars == 280
    assert d.wanted_visuals == 1 and d.extra_visuals == []
    with pytest.raises(SchemaError, match="every draft needs a visual"):
        validate_output(out(chart=None))
    with pytest.raises(SchemaError, match="3-6 posts"):
        validate_output(out(thread=["a", "b"]))
    with pytest.raises(SchemaError, match="carries 1 visual"):
        validate_output(out(visuals=[CHART2]))


def test_validate_output_reads_the_format():
    two = Format(visuals=2, anchors=("first", "last"))
    d = validate_output(out(visuals=[CHART2]), two)
    assert len(d.visuals) == 2 and d.anchors == [1, 3] and d.wanted_visuals == 2
    assert d.extra_visuals[0].title == "Cohort"
    with pytest.raises(SchemaError, match="carries 2 visual"):
        validate_output(out(), two)
    none = Format(visuals=0, anchors=())
    d = validate_output(out(chart=None), none)
    assert d.visuals == [] and d.anchors == [] and d.wanted_visuals == 0
    with pytest.raises(SchemaError, match="no visual"):
        validate_output(out(), none)
    single = validate_output(out(thread=["all in one"]), single_format())
    assert single.shape == "single" and single.anchors == [1]
    with pytest.raises(SchemaError, match="single post is 1 post"):
        validate_output(out(), single_format())
    long = validate_output(out(thread=["a\n\nb\n\nc"]), long_format(2000))
    assert long.shape == "long" and long.max_chars == 2000
    with pytest.raises(SchemaError, match="empty chart"):
        validate_output(out(visuals=[None]), two)


def test_no_shape_carries_a_link():
    """Rule 2: no post carries a link, so a single or long post is exactly one post and
    the source is named in words."""
    fmt = single_format()
    ok = validate_output(out(thread=["the claim, no link here"]), fmt)
    assert drafter.check_hard_rules(ok, url=URL, source="pubmed", fmt=fmt) == []

    linked = validate_output(out(thread=[f"the claim {URL}"]), fmt)
    problems = drafter.check_hard_rules(linked, url=URL, source="pubmed", fmt=fmt)
    assert any("thread[0] contains a link" in p for p in problems)

    other_link = validate_output(out(thread=["the claim, see example.com/x"]), fmt)
    problems = drafter.check_hard_rules(other_link, url=URL, source="pubmed", fmt=fmt)
    assert any("thread[0] contains a link" in p for p in problems)

    # the long post's body is not held to the hook's 220-character cap
    long_body = "x" * 900
    d = validate_output(out(thread=[long_body]), long_format(4000))
    assert drafter.check_hard_rules(d, url=URL, source="pubmed", fmt=long_format(4000)) == []
    assert long_format(4000).resolve_anchors(1) == [1]


# ---- hard rules and prompt --------------------------------------------------------


def test_hard_rules_use_the_format_limit_and_check_every_chart():
    d = validate_output(out(thread=["x" * 300]), long_format(1000))
    assert drafter.check_hard_rules(d, url=URL, source="pubmed") == []
    # read as a thread instead, the same draft fails the per-post limit and the hook cap
    assert drafter.check_hard_rules(d, url=URL, source="pubmed", fmt=Format()) == [
        f"thread[0] is 300 chars (> {MAX_POST_CHARS})",
        "first post is 300 chars (> 220); the opener is one claim, not a summary",
    ]
    over = validate_output(out(thread=["x" * 1200]), long_format(1000))
    assert drafter.check_hard_rules(over, url=URL, source="pubmed") == [
        "thread[0] is 1200 chars (> 1000)"
    ]
    two = validate_output(
        out(visuals=[{**CHART2, "values": [88, 99]}]),
        Format(visuals=2, anchors=("first", "last")),
    )
    assert drafter.verify_chart(two, ABSTRACT) == ["99"]
    assert "99" in drafter.chart_problems(two, ABSTRACT)[0]


def test_hard_rules_text_per_format():
    assert hard_rules(None) == HARD_RULES
    assert "exactly one visual" in HARD_RULES and "3 to 6 posts" in HARD_RULES
    single = hard_rules(single_format())
    assert "SINGLE post" in single and "exactly one string" in single
    assert "NEVER write a URL" in single
    long = hard_rules(long_format(4000))
    assert "long-form post of at most 4000" in long
    two = hard_rules(Format(visuals=2, anchors=("first", "last")))
    assert "carries 2 visuals" in two
    none = hard_rules(Format(visuals=0, anchors=()))
    assert "NO visual" in none
    assert build_system_prompt(None, None) == build_system_prompt()
    assert "SINGLE post" in build_system_prompt(None, single_format())


def test_format_of_a_stored_draft():
    assert drafter.format_of(validate_output(out())) is None
    d = validate_output(out(chart=None), Format(visuals=0, anchors=()))
    assert drafter.format_of(d) == Format(visuals=0, anchors=())
    d = validate_output(out(thread=["one"]), long_format(3000))
    assert drafter.format_of(d).shape == "long" and drafter.format_of(d).max_chars == 3000
    # a table dropped by the reviewer does not change what the format asked for
    d = validate_output(out())
    d.chart = None
    assert drafter.format_of(d) is None


def test_format_of_preserves_where_each_picture_was_anchored():
    # A single-visual draft anchored to post 1 is unchanged.
    d = validate_output(out())
    assert drafter.format_of(d) is None
    fmt = Format(visuals=2, anchors=("first", "last"))
    d = validate_output(out(visuals=[CHART2]), fmt)
    assert d.anchors == [1, 3]  # resolved against this draft's own 3-post thread
    rebuilt = drafter.format_of(d)
    # The second picture was anchored to the LAST post, not literally post 3: re-resolving
    # against a revision of a different length must still land it on the new last post.
    assert rebuilt.resolve_anchors(6) == [1, 6]


def test_draft_item_passes_the_format_to_the_checks():
    calls = []

    def call(system, user, model):
        calls.append(system)
        return json.dumps(out(thread=["one post"]))

    res = drafter.draft_item(
        title="T",
        abstract=ABSTRACT,
        url=URL,
        source="pubmed",
        call=call,
        sleep=lambda s: None,
        fmt=single_format(),
    )
    assert res.draft.shape == "single" and "SINGLE post" in calls[0]
    with pytest.raises(drafter.DraftRejected):
        drafter.draft_item(
            title="T",
            abstract=ABSTRACT,
            url=URL,
            source="pubmed",
            call=call,
            sleep=lambda s: None,
            max_attempts=1,
        )


# ---- storage --------------------------------------------------------------------


def test_store_round_trips_format_and_extra_visuals_and_images(conn):
    seed_item(conn, "i1")
    d = validate_output(
        out(visuals=[CHART2], thread=["one", "two", "three", "four", "five"]),
        Format(visuals=2, anchors=("first", "last")),
    )
    did = qstore.insert_draft(conn, item_id="i1", model="m", draft=d)
    row = qstore.get_draft(conn, did)
    assert row.draft.anchors == [1, 5] and row.draft.wanted_visuals == 2
    assert [c.title for c in row.draft.extra_visuals] == ["Cohort"]
    p0, p1 = qstore.image_file(did), qstore.image_file(did, 1)
    p0.parent.mkdir(parents=True, exist_ok=True)
    p0.write_bytes(b"a")
    p1.write_bytes(b"b")
    qstore.set_image(conn, did, p0, "first alt")
    qstore.set_image(conn, did, p1, "second alt", index=1)
    row = qstore.get_draft(conn, did)
    assert row.image_path == f"draft_{did}.png" and row.image_alt == "first alt"
    assert [(i["index"], i["anchor"], i["alt"]) for i in row.images] == [
        (0, 1, "first alt"),
        (1, 5, "second alt"),
    ]
    # publish sees both, at their anchors
    qstore.approve(conn, did)
    got = pstore.fetch_approved(10, conn=conn)[0]
    assert got.shape == "thread" and got.max_chars == 280
    assert [(a, anchor) for _, a, anchor in got.images] == [("first alt", 1), ("second alt", 5)]
    # revise clears the pictures and keeps the shape
    qstore.revise(
        conn,
        did,
        draft=validate_output(out(visuals=[CHART2]), Format(visuals=2, anchors=("first", "last"))),
        model="m",
        note="n",
    )
    row = qstore.get_draft(conn, did)
    assert row.images == [] and row.image_path is None and row.draft.anchors == [1, 3]
    # drop removes every picture and the extra charts
    qstore.set_image(conn, did, p0, "first alt")
    qstore.set_image(conn, did, p1, "second alt", index=1)
    qstore.drop_image(conn, did)
    row = qstore.get_draft(conn, did)
    assert row.images == [] and row.draft.extra_visuals == [] and not p1.exists()


def _two_picture_draft(conn, item="i1"):
    """A two-visual draft with both pictures on disk: (draft id, first path, second path)."""
    seed_item(conn, item)
    d = validate_output(
        out(visuals=[CHART2], thread=["one", "two", "three", "four", "five"]),
        Format(visuals=2, anchors=("first", "last")),
    )
    did = qstore.insert_draft(conn, item_id=item, model="m", draft=d)
    p0, p1 = qstore.image_file(did), qstore.image_file(did, 1)
    p0.parent.mkdir(parents=True, exist_ok=True)
    p0.write_bytes(b"a")
    p1.write_bytes(b"b")
    qstore.set_image(conn, did, p0, "first alt")
    qstore.set_image(conn, did, p1, "second alt", index=1)
    return did, p0, p1


def test_drop_image_by_index_keeps_the_other_one(conn):
    # dropping the second picture leaves the first one and its chart alone
    did, p0, p1 = _two_picture_draft(conn)
    qstore.drop_image(conn, did, note="second one is noise", index=1)
    row = qstore.get_draft(conn, did)
    assert row.draft.extra_visuals == [] and row.draft.anchors == [1]
    assert row.draft.chart is not None and row.image_path == f"draft_{did}.png"
    assert [(i["index"], i["alt"]) for i in row.images] == [(0, "first alt")]
    assert p0.exists() and not p1.exists()
    dec = qstore.list_decisions(conn, did)
    assert dec[-1]["action"] == "edit" and dec[-1]["note"] == "second one is noise"
    assert row.draft.thread[0] == "one"  # text untouched
    # publish sees one picture, on its own post
    qstore.approve(conn, did)
    got = pstore.fetch_approved(10, conn=conn)[0]
    assert [(a, anchor) for _, a, anchor in got.images] == [("first alt", 1)]


def test_dropping_the_first_picture_promotes_the_second(conn):
    did, p0, p1 = _two_picture_draft(conn)
    qstore.drop_image(conn, did, index=0)
    row = qstore.get_draft(conn, did)
    # the extra chart becomes the draft's visual and its picture becomes the first one
    assert row.draft.chart.title == "Cohort" and row.draft.extra_visuals == []
    assert row.draft.anchors == [5]
    assert row.image_path == p1.name and row.image_alt == "second alt"
    assert [(i["index"], i["anchor"], i["alt"]) for i in row.images] == [(0, 5, "second alt")]
    assert p1.exists() and not p0.exists()
    assert qstore.list_decisions(conn, did)[-1]["note"] == "image 1 dropped"
    qstore.approve(conn, did)
    got = pstore.fetch_approved(10, conn=conn)[0]
    assert [(a, anchor) for _, a, anchor in got.images] == [("second alt", 5)]


def test_dropping_every_picture_one_at_a_time_ends_where_drop_all_does(conn):
    did, p0, p1 = _two_picture_draft(conn)
    qstore.drop_image(conn, did, index=1)
    qstore.drop_image(conn, did, index=0)
    row = qstore.get_draft(conn, did)
    assert row.images == [] and row.image_path is None and row.image_alt == ""
    assert row.draft.visual is None and row.draft.extra_visuals == []
    assert not p0.exists() and not p1.exists()
    with pytest.raises(IndexError):
        qstore.drop_image(conn, did, index=0)


def test_queue_drops_one_picture_of_two(conn):
    from fastapi.testclient import TestClient

    from approval_queue.app import app

    did, p0, p1 = _two_picture_draft(conn)
    with TestClient(app) as client:
        page = client.get(f"/drafts/{did}").text
        assert f"/drafts/{did}/image/0/drop" in page and f"/drafts/{did}/image/1/drop" in page
        r = client.post(f"/drafts/{did}/image/1/drop", data={"note": "one is enough"})
        assert r.status_code == 200
        assert client.post(f"/drafts/{did}/image/7/drop").status_code == 404
    row = qstore.get_draft(conn, did)
    assert [i["index"] for i in row.images] == [0] and p0.exists() and not p1.exists()


def test_long_post_survives_publish_checks_and_single_is_not_numbered(conn):
    long_text = "x" * 900
    assert split_thread([long_text], max_chars=4000, number=False) == [long_text]
    with pytest.raises(ThreadError, match="> 280"):
        split_thread([long_text])
    seed_item(conn, "i2")
    d = validate_output(out(thread=[long_text]), long_format(4000))
    did = qstore.insert_draft(conn, item_id="i2", model="m", draft=d)
    qstore.approve(conn, did)
    got = pstore.fetch_approved(10, conn=conn)[0]
    assert got.shape == "long" and got.max_chars == 4000
    import run_publish

    kind, texts = run_publish.texts_for(got)
    assert texts == [long_text]


def test_publish_attaches_each_picture_at_its_anchor(tmp_path):
    import run_publish

    uploads, posts = [], []

    class C:
        PublishError = run_publish.client.PublishError

        @staticmethod
        def upload_media(path, alt):
            uploads.append((path, alt))
            return f"m{len(uploads)}"

        @staticmethod
        def post_tweet(text, in_reply_to=None, media_ids=None):
            posts.append((text, media_ids))
            return f"t{len(posts)}"

    class S:
        SCHED_POSTED = "posted"
        SCHED_FAILED = "failed"
        SCHED_PARTIAL = "partial"
        recorded = []

        @staticmethod
        def record_post(conn, **kw):
            S.recorded.append(kw)

        @staticmethod
        def finish(conn, draft_id, status, error=None):
            S.status = status

    import types

    app = types.SimpleNamespace(draft_id=1, image_alt="")
    orig_client, orig_store = run_publish.client, run_publish.store
    run_publish.client, run_publish.store = C, S
    try:
        images = {1: [("a.png", "A")], 3: [("b.png", "B")], 9: [("c.png", "C")]}
        status = run_publish.publish_one(
            None, app, "thread", ["p1", "p2", "p3"], "s", None, images=images
        )
    finally:
        run_publish.client, run_publish.store = orig_client, orig_store
    assert status == "posted"
    assert [m for _, m in posts] == [["m1"], None, ["m2", "m3"]]  # past-the-end goes last
    assert uploads == [("a.png", "A"), ("b.png", "B"), ("c.png", "C")]
    assert run_publish.images_for(
        types.SimpleNamespace(images=[], image_path="x.png", image_alt="alt"), {}
    ) == {1: [("x.png", "alt")]}
    assert (
        run_publish.images_for(
            types.SimpleNamespace(
                images=[("x.png", "alt", 2)], image_path="x.png", image_alt="alt"
            ),
            {"media": {"attach_images": False}},
        )
        == {}
    )


# ---- the swarm on a single and a long post -----------------------------------------


def test_run_swarm_single_post_runs_one_cell():
    fake = FakeModel()
    brief = Brief(title="T", abstract=ABSTRACT, url=URL, source="pubmed")
    res = engine.run_swarm(
        brief, DEFAULT_GENOME, CFG, call=fake, sleep=lambda s: None, fmt=single_format()
    )
    assert list(res.cells) == [SINGLE_SLOT.name]
    assert res.draft_result.draft.shape == "single"
    thread = res.draft_result.draft.thread
    assert len(thread) == 1 and URL not in thread[0]
    cell_prompts = [u for s, u, _ in fake.calls if "YOUR SLOT: single" in u]
    assert cell_prompts and all("carries NO link of any kind" in u for u in cell_prompts)


def test_run_swarm_long_post_joins_sections_under_the_section_limit():
    fake = FakeModel()
    brief = Brief(title="T", abstract=ABSTRACT, url=URL, source="pubmed")
    cfg = {**CFG, "formats": {"long_max_chars": 4000, "long_section_chars": 500}}
    res = engine.run_swarm(
        brief, DEFAULT_GENOME, cfg, call=fake, sleep=lambda s: None, fmt=long_format(4000)
    )
    assert res.draft_result.draft.shape == "long"
    thread = res.draft_result.draft.thread
    assert len(thread) == 1 and URL not in thread[0]
    assert thread[0].count("\n\n") == len(DEFAULT_GENOME.slots) - 1
    closer = next(u for _, u, _ in fake.calls if "YOUR SLOT: closer" in u)
    assert "carries NO link of any kind" in closer
    systems = {s for s, _, _ in fake.calls if "section of a long post" in s}
    assert systems and all("At most 500 characters" in s for s in systems)
    assembly = next(u for _, u, _ in fake.calls if "Assemble the draft JSON" in u)
    assert "joined by blank lines" in assembly and "under 4000 characters" in assembly
    assert "exactly one string" in assembly


# ---- format genomes -----------------------------------------------------------------


def test_seed_formats_convert_and_breed():
    names = [f.name for f in SEED_FORMATS]
    assert names == ["thread-1-first", "thread-2-ends", "thread-0", "single-1", "long-1"]
    for f in SEED_FORMATS:
        fmt = f.to_format(4000)
        assert fmt.visuals == f.visuals
    assert SEED_FORMATS[1].to_format(4000).anchors == ("first", "last")
    assert SEED_FORMATS[4].to_format(2500).max_chars == 2500
    g = FormatGenome.from_json(SEED_FORMATS[1].to_json(), id=7)
    assert g.id == 7 and g.anchors == ["first", "last"]
    for seed in range(30):
        parent = random.Random(seed).choice(SEED_FORMATS)
        child = mutate.breed_format(parent, {"thread-3-6-1f"}, random.Random(seed))
        child.to_format(4000)  # every child is a valid Format
        assert child.parent_id == parent.id and child.notes
        assert child.name != "thread-3-6-1f"
    assert mutate.format_name(SEED_FORMATS[1]) == "thread-3-6-2fl"
    assert mutate.format_name(SEED_FORMATS[3]) == "single-1-1f"
    n = len(mutate.format_neighbours(SEED_FORMATS[2]))
    assert n == 6  # two shapes, +1 visual, four post-range steps
