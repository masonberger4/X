"""A studio piece in the approval queue: listed and shown as the studio's, revised only in
the studio, edited against its own per-post limit, every card shown with the post it goes
on, and handed to step 3 whole.

Each draft gets into the queue the way the studio puts it there (studio/ingest.py:to_queue,
from the checker's report over a finished piece's files and cards); no model is involved.
"""

import struct
import zlib
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from approval_queue import store
from approval_queue.app import app
from draft import drafter
from draft.schema import Draft
from publish import store as publish_store
from studio import ingest, qa
from studio import store as studio_store
from tests.conftest import seed_item

POSTS = [
    "Next-gen CTLA-4 is not ipilimumab again. Fc-enhanced binders deplete Tregs in the tumour "
    "and spare the periphery, and that is the whole bet.",
    "The data so far: response rates in cold tumours where ipilimumab did little.",
    "What to watch: the randomised readouts due next year.",
]


def png(width: int = 4, height: int = 5) -> bytes:
    """A small, valid PNG (a card the renderer drew, as far as the queue can tell)."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    rows = b"".join(b"\x00" + b"\x20\x40\x80" * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def put_piece(conn, tmp_path, posts, cards=(), *, title="Next-gen CTLA-4"):
    """A finished studio piece in the queue: its studio_pieces row, its folder with one PNG
    per card, and the checker's report, through studio/ingest.py. `cards` are (post, alt).
    Returns (piece id, draft id, the card files)."""
    studio_store.ensure_tables(conn)
    workspace = tmp_path / f"piece-{len(list(tmp_path.glob('piece-*')))}"
    (workspace / "cards").mkdir(parents=True)
    piece_id = studio_store.create_piece(
        conn,
        origin="manual",
        topic=title,
        cluster_id=None,
        requested_angle="",
        checkpoint=False,
        session_id="5b0e2c1a-0000-4000-8000-000000000001",
        workspace=str(workspace),
        model="claude-opus-5-5",
        effort="max",
    )
    piece = studio_store.get_piece(conn, piece_id)
    files = []
    for n in range(1, len(cards) + 1):
        card = workspace / "cards" / f"card_{n}.png"
        card.write_bytes(png(4, 4 + n))  # each card its own bytes
        card.with_suffix(".html").write_text("<html></html>", encoding="utf-8")
        files.append(card)
    report = qa.Report(
        piece=qa.PieceFiles(
            title=title,
            angle="contrarian",
            shape="long",
            posts=list(posts),
            cards=[
                qa.Card(html=f.with_suffix(".html"), png=f, post=post, alt=alt)
                for f, (post, alt) in zip(files, cards, strict=True)
            ],
            summary="Why the next CTLA-4 wave is a different drug class.",
        )
    )
    draft_id = ingest.to_queue(conn, piece, report, max_chars=25000)
    return piece_id, draft_id, files


def drafter_draft(conn, item_id="i1", **over):
    seed_item(conn, item_id, source="biorxiv")
    draft = Draft(thread=["one", "two", "three"], suggested_visual="", why_it_matters="")
    for key, value in over.items():
        setattr(draft, key, value)
    return store.insert_draft(conn, item_id=item_id, model="m", draft=draft)


def row_of(body: str, draft_id: int) -> str:
    """The list row of one draft on a queue page."""
    rows = [r for r in body.split("<tr>") if f'href="/drafts/{draft_id}"' in r]
    assert len(rows) == 1, f"draft {draft_id} is listed {len(rows)} times"
    return rows[0].split("</tr>")[0]


@pytest.fixture
def client(db_file, monkeypatch):
    # db_file sets DB_PATH: the app's connections, image_dir() and to_queue's copies all
    # land in the test's folder. The queue as the control panel serves it, with the studio
    # pages beside it (the template environment is process-wide, so it is set here).
    from approval_queue import app as queue_app

    monkeypatch.setitem(queue_app.templates.env.globals, "HAS_PANEL", True)
    return TestClient(app, follow_redirects=False)


def test_the_standalone_queue_names_a_studio_draft_without_dead_links(
    client, conn, tmp_path, monkeypatch
):
    # run_queue.py serves the queue alone: no /studio pages, so no links to them
    from approval_queue import app as queue_app

    monkeypatch.setitem(queue_app.templates.env.globals, "HAS_PANEL", False)
    piece_id, draft_id, _ = put_piece(conn, tmp_path, POSTS)
    listing = row_of(client.get("/queue").text, draft_id)
    detail = client.get(f"/drafts/{draft_id}").text
    assert f"<strong>Studio piece {piece_id}</strong>" in listing
    assert 'href="/studio/' not in listing and 'href="/studio/' not in detail
    assert "its studio page in the control panel (run_app.py)" in detail


def test_the_pending_list_names_a_studio_draft_by_its_piece(client, conn, tmp_path):
    plain = drafter_draft(conn)
    piece_id, draft_id, _ = put_piece(conn, tmp_path, POSTS, [(1, "Treg depletion by binder")])
    body = client.get("/queue").text
    row = row_of(body, draft_id)
    assert f"<strong>Studio piece {piece_id}</strong>" in row
    assert f'<a href="/studio/{piece_id}">fact base, fact-check log and Revise' in row
    # revised in the studio only: no drafter Revise box on its row
    assert f'action="/drafts/{draft_id}/revise"' not in body
    # a drafter draft on the same page keeps its title and its own Revise box
    assert "Title i1" in row_of(body, plain) and f'action="/drafts/{plain}/revise"' in body
    assert "/studio/" not in row_of(body, plain)


def test_the_detail_page_names_the_piece_and_links_its_studio_page(client, conn, tmp_path):
    piece_id, draft_id, _ = put_piece(
        conn, tmp_path, POSTS, [(1, "Treg depletion by binder"), (3, "Readouts due next year")]
    )
    body = client.get(f"/drafts/{draft_id}").text
    assert f"<h1>Studio piece {piece_id}</h1>" in body
    assert f'Written in the <a href="/studio/{piece_id}">studio</a>' in body
    assert "claude-opus-5-5 (studio)" in body
    assert "Format: <strong>3 long posts, each up to 25000 chars</strong>, 2 pictures." in body
    assert "format genome" not in body
    assert "Designed and checked in the studio session" in body
    # Revise is a link to the studio page, never the drafter's form
    assert f'Revise this piece on <a href="/studio/{piece_id}">its studio page</a>' in body
    assert f'action="/drafts/{draft_id}/revise"' not in body
    # the cards come with no chart or table spec, so there is nothing to redraw
    assert f'action="/drafts/{draft_id}/image/redraw"' not in body
    # the editor can still edit by hand, approve and reject here
    for action in ("edit", "approve", "reject"):
        assert f'action="/drafts/{draft_id}/{action}"' in body


def test_a_drafter_draft_keeps_its_title_and_its_revise_form(client, conn):
    draft_id = drafter_draft(conn)
    body = client.get(f"/drafts/{draft_id}").text
    assert "<h1>Title i1</h1>" in body and "Studio piece" not in body
    assert f'action="/drafts/{draft_id}/revise"' in body and "/studio/" not in body


def test_the_queue_never_revises_a_studio_draft_itself(client, conn, tmp_path, monkeypatch):
    piece_id, draft_id, _ = put_piece(conn, tmp_path, POSTS)

    def no_drafter(**kwargs):
        raise AssertionError("the drafter was asked to revise a studio piece")

    monkeypatch.setattr(drafter, "revise_item", no_drafter)
    monkeypatch.setattr(drafter, "call_anthropic", no_drafter)
    for form in ({"instructions": "lead with the cold-tumour data"}, {}):
        r = client.post(f"/drafts/{draft_id}/revise", data=form)
        assert r.status_code == 303
        location = unquote(r.headers["location"])
        assert location.startswith(f"/drafts/{draft_id}?error=")
        assert "written in the studio" in location and f"(/studio/{piece_id})" in location
    row = store.get_draft(conn, draft_id)
    assert row.draft.thread == POSTS and row.status == "pending"
    assert store.list_decisions(conn, draft_id) == []
    page = client.get(r.headers["location"]).text
    assert "This piece was written in the studio; revise it from its studio page" in page


def test_editing_a_studio_post_is_held_to_its_own_limit(client, conn, tmp_path):
    _, draft_id, _ = put_piece(conn, tmp_path, POSTS[:1])
    five_thousand = "Next-gen CTLA-4, in full. " + "x" * 4974
    assert len(five_thousand) == 5000
    r = client.post(f"/drafts/{draft_id}/edit", data={"thread": five_thousand, "keep_pending": 1})
    assert r.status_code == 303 and r.headers["location"] == f"/drafts/{draft_id}"
    row = store.get_draft(conn, draft_id)
    assert row.draft.thread == [five_thousand] and row.status == "pending"
    assert (row.draft.shape, row.draft.max_chars) == ("long", 25000)  # the format is kept
    # the limit itself: 25000 fits, 25001 does not
    r = client.post(f"/drafts/{draft_id}/edit", data={"thread": "y" * 25000, "keep_pending": 1})
    assert r.status_code == 303
    r = client.post(f"/drafts/{draft_id}/edit", data={"thread": "z" * 25001, "keep_pending": 1})
    assert r.status_code == 400
    assert "1 post(s) exceed 25000 characters (post 1 is 25001 characters)" in r.text
    assert store.get_draft(conn, draft_id).draft.thread == ["y" * 25000]


def test_editing_a_drafter_draft_is_still_held_to_280(client, conn):
    draft_id = drafter_draft(conn)
    r = client.post(f"/drafts/{draft_id}/edit", data={"thread": "x" * 300 + "\n---\nb"})
    assert r.status_code == 400
    assert "exceed 280 characters (post 1 is 300 characters)" in r.text
    assert store.get_draft(conn, draft_id).draft.thread == ["one", "two", "three"]
    assert store.list_decisions(conn, draft_id) == []


def test_any_draft_is_edited_against_its_own_limit_but_never_below_280(client, conn):
    # a step 9 long post (a format genome's limit)
    long_id = drafter_draft(conn, "i1", shape="long", max_chars=4000, anchors=[1])
    r = client.post(f"/drafts/{long_id}/edit", data={"thread": "x" * 4000, "keep_pending": 1})
    assert r.status_code == 303
    r = client.post(f"/drafts/{long_id}/edit", data={"thread": "x" * 4001, "keep_pending": 1})
    assert r.status_code == 400 and "exceed 4000 characters" in r.text
    # a stored limit under X's own 280 is never applied
    odd_id = drafter_draft(conn, "i2", max_chars=100)
    r = client.post(f"/drafts/{odd_id}/edit", data={"thread": "x" * 280, "keep_pending": 1})
    assert r.status_code == 303


def test_the_detail_page_counts_each_post_against_the_drafts_limit(client, conn, tmp_path):
    _, draft_id, _ = put_piece(conn, tmp_path, ["a" * 5000, "b" * 26000])
    body = client.get(f"/drafts/{draft_id}").text
    assert '<div class="meta ">5000/25000</div>' in body
    assert '<div class="meta over">26000/25000</div>' in body
    assert "/280</div>" not in body
    assert "<h2>Thread</h2>" in body and "1/2 aaaa" in body and "2/2 bbbb" in body
    # a drafter draft still counts to 280
    plain = drafter_draft(conn)
    assert '<div class="meta ">3/280</div>' in client.get(f"/drafts/{plain}").text


def test_a_single_long_post_reads_as_one_post(client, conn, tmp_path):
    _, draft_id, _ = put_piece(conn, tmp_path, POSTS[:1])
    body = client.get(f"/drafts/{draft_id}").text
    assert "<h2>Post</h2>" in body and "<h2>Thread</h2>" not in body
    assert f'<div class="post">{POSTS[0]}</div>' in body  # no "1/1" in front of it
    assert "Format: <strong>long post up to 25000 chars</strong>, 0 pictures." in body


def test_every_card_is_shown_with_the_post_it_goes_on(client, conn, tmp_path):
    alts = ["Treg depletion by binder", "Response in cold tumours", "Readouts due next year"]
    _, draft_id, files = put_piece(conn, tmp_path, POSTS, list(zip([1, 2, 3], alts, strict=True)))
    body = client.get(f"/drafts/{draft_id}").text
    assert "Attached to post 1 when published." in body
    assert "Picture 2, attached to post 2 when published." in body
    assert "Picture 3, attached to post 3 when published." in body
    assert "Second picture" not in body
    assert "Drop all pictures" in body and "Drop both images" not in body
    assert f'src="/drafts/{draft_id}/image?v=' in body
    for k in (1, 2):
        assert f'src="/drafts/{draft_id}/image/{k}?v=' in body
        assert f'action="/drafts/{draft_id}/image/{k}/drop"' in body
    for alt in alts:
        assert f'alt="{alt}"' in body
    # each picture is the queue's own copy of the card, served as a PNG
    urls = [f"/drafts/{draft_id}/image"] + [f"/drafts/{draft_id}/image/{k}" for k in (1, 2)]
    for url, card in zip(urls, files, strict=True):
        r = client.get(url)
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
        assert r.content == card.read_bytes()
    assert client.get(f"/drafts/{draft_id}/image/3").status_code == 404
    assert all(f.exists() for f in files)  # copies: the piece's own cards stay where they are


def test_two_cards_read_as_a_second_picture_and_one_card_as_the_image(client, conn, tmp_path):
    _, two, _ = put_piece(conn, tmp_path, POSTS, [(1, "first card"), (3, "last card")])
    body = client.get(f"/drafts/{two}").text
    assert "Second picture, attached to post 3 when published." in body
    assert "Drop both images" in body and "Picture 2," not in body
    _, one, _ = put_piece(conn, tmp_path, POSTS, [(2, "the only card")])
    body = client.get(f"/drafts/{one}").text
    assert "Attached to post 2 when published." in body
    assert "Second picture" not in body and "Picture 2," not in body
    assert "<button>Drop image</button>" in body


def test_dropping_a_middle_card_keeps_the_others_on_their_posts(client, conn, tmp_path):
    cards = [(1, "first card"), (2, "middle card"), (3, "last card")]
    _, draft_id, _ = put_piece(conn, tmp_path, POSTS, cards)
    middle = store.image_file(draft_id, 1)
    assert middle.exists()
    r = client.post(f"/drafts/{draft_id}/image/1/drop", data={"note": "says the same as card 1"})
    assert r.status_code == 303
    row = store.get_draft(conn, draft_id)
    assert [(im["alt"], im["anchor"]) for im in row.images] == [
        ("first card", 1),
        ("last card", 3),
    ]
    assert row.draft.anchors == [1, 3] and not middle.exists()
    body = client.get(f"/drafts/{draft_id}").text
    assert "Second picture, attached to post 3 when published." in body
    assert "Drop both images" in body and 'alt="middle card"' not in body
    assert row.draft.thread == POSTS  # the text is untouched


def test_an_approved_studio_draft_reaches_the_publisher_whole(client, conn, tmp_path):
    piece_id, draft_id, _ = put_piece(
        conn, tmp_path, POSTS[:2], [(1, "Treg depletion by binder"), (2, "Readouts")]
    )
    edited = POSTS[0] + " And the cold-tumour data is the reason to care." * 60
    assert len(edited) > 280
    r = client.post(f"/drafts/{draft_id}/edit", data={"thread": f"{edited}\n---\n{POSTS[1]}"})
    assert r.status_code == 303 and r.headers["location"] == "/queue"  # saved and approved
    assert store.get_draft(conn, draft_id).status == "approved"
    (approved,) = publish_store.fetch_approved(conn=conn)
    assert approved.draft_id == draft_id and approved.edited
    assert approved.thread == [edited, POSTS[1]]
    assert (approved.shape, approved.max_chars) == ("long", 25000)
    assert [(Path(p).name, alt, post) for p, alt, post in approved.images] == [
        (f"draft_{draft_id}.png", "Treg depletion by binder", 1),
        (f"draft_{draft_id}_2.png", "Readouts", 2),
    ]
    listed = client.get("/status/approved").text
    assert f"<strong>Studio piece {piece_id}</strong>" in row_of(listed, draft_id)
    assert "fact base, fact-check log and Revise" not in listed  # only while it is pending
