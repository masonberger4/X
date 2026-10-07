"""A draft's format: schema bounds, storage of shape and pictures, picture drops and
publishing anchors."""

import pytest

from approval_queue import store as qstore
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
from tests.conftest import seed_item

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


# ---- hard rules and prompt --------------------------------------------------------


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
