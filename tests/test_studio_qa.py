"""The app's check of a finished studio piece (studio/qa.py): reading the session's folder,
the text rules, the side files and the cards, drawn by a stand-in renderer (and once, at
the end, by the real one where a browser can draw).
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

import pytest

from studio import qa, render, safety
from studio.angles import load_angles
from studio.settings import DEFAULTS

XCFG = dict(DEFAULTS["x"])  # long and thread posts 25000, short 1000, headroom 50, 4 cards
CLOSING = "Not investment advice."
POST = "Merck paid $400 million upfront for the bispecific. " + CLOSING
CARD = {"file": "cards/card_1.html", "post": 1, "type": "headline_stat", "alt": "A chart"}


def png_header(w: int, h: int) -> bytes:
    """The first 24 bytes of a PNG, all that render.png_size reads."""
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + w.to_bytes(4, "big") + h.to_bytes(4, "big")


def folder(
    root: Path,
    *,
    texts: tuple[str, ...] = (POST,),
    card_files: int = 0,
    side_files: bool = True,
    **fields,
) -> Path:
    """A piece folder the way the write stage leaves it: posts/01.txt and on holding `texts`,
    `card_files` cards, the fact base and fact-check log, and piece.json listing them.
    `fields` replace keys of piece.json (posts and cards included)."""
    (root / "posts").mkdir(parents=True, exist_ok=True)
    posts = []
    for i, text in enumerate(texts, start=1):
        posts.append(f"posts/{i:02d}.txt")
        (root / posts[-1]).write_text(text, encoding="utf-8")
    cards = []
    for i in range(1, card_files + 1):
        (root / "cards").mkdir(exist_ok=True)
        (root / f"cards/card_{i}.html").write_text("<p>card</p>", encoding="utf-8")
        cards.append({**CARD, "file": f"cards/card_{i}.html"})
    data = {
        "title": "Merck buys a bispecific",
        "angle": "deal_decoder",
        "angle_reason": "the price is the story",
        "shape": "long_post" if len(texts) == 1 else "thread",
        "hook_style": "hard_number",
        "posts": posts,
        "cards": cards,
        "companies": [{"name": "Merck", "ticker": "MRK"}],
        "handles": [],
        "recheck_before_posting": [],
        "summary": "Two sentences for the editor.",
        **fields,
    }
    (root / "piece.json").write_text(json.dumps(data), encoding="utf-8")
    if side_files:
        (root / "factbase.md").write_text("# Fact base\n- $400 million upfront\n", "utf-8")
        (root / "factcheck.md").write_text("| claim | source | verdict |\n", "utf-8")
    return root


def read(root: Path):
    piece, blocking, minor = qa.read_piece(root)
    assert piece is not None, blocking
    return piece, blocking, minor


# ---- read_piece ----------------------------------------------------------------------------


def test_a_missing_piece_json_blocks(tmp_path):
    assert qa.read_piece(tmp_path) == (
        None,
        ["piece.json is missing; write it as the write stage describes"],
        [],
    )


@pytest.mark.parametrize(
    "text, problem",
    [
        ("{", "piece.json is not valid JSON"),
        ("", "piece.json is not valid JSON"),
        ("[1, 2]", "piece.json must be a JSON object"),
        ('"a piece"', "piece.json must be a JSON object"),
    ],
)
def test_piece_json_that_is_not_an_object_blocks(tmp_path, text, problem):
    (tmp_path / "piece.json").write_text(text, encoding="utf-8")
    piece, blocking, minor = qa.read_piece(tmp_path)
    assert piece is None and minor == []
    [only] = blocking
    assert only.startswith(problem)


def test_a_byte_order_mark_is_fine_in_piece_json_and_in_the_posts(tmp_path):
    folder(tmp_path, texts=("one", "two"))
    for name in ("piece.json", "posts/01.txt"):
        path = tmp_path / name
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    piece, blocking, minor = read(tmp_path)
    assert (blocking, minor) == ([], [])
    assert piece.posts == ["one", "two"] and piece.title == "Merck buys a bispecific"


def test_posts_are_read_in_order_trimmed_with_their_files(tmp_path):
    folder(tmp_path, texts=("\n  First post.  \n\n", "Second post.\n"))
    piece, blocking, _ = read(tmp_path)
    assert blocking == []
    assert piece.posts == ["First post.", "Second post."]
    assert piece.post_files == ["posts/01.txt", "posts/02.txt"]


@pytest.mark.parametrize(
    "rel",
    ["../outside.txt", "posts/../../outside.txt", "{outside}", "posts/\x0001.txt", ""],
)
def test_a_post_path_must_stay_inside_the_folder(tmp_path, rel):
    (tmp_path / "outside.txt").write_text("a file the session must not reach", "utf-8")
    rel = rel.format(outside=tmp_path / "outside.txt")
    work = folder(tmp_path / "piece", texts=("one",), posts=["posts/01.txt", rel])
    piece, blocking, _ = read(work)
    assert blocking == [f"piece.json: post 2 path {rel!r} is not inside your folder"]
    assert piece.posts == ["one"]  # the outside file was never read


def test_a_path_that_wanders_but_ends_inside_is_fine(tmp_path):
    folder(tmp_path, texts=("one",), posts=["posts/../posts/01.txt"])
    piece, blocking, _ = read(tmp_path)
    assert (piece.posts, blocking) == (["one"], [])


def test_an_empty_missing_or_unreadable_post_blocks(tmp_path):
    folder(
        tmp_path,
        texts=("one", " \n\t ", "three"),
        posts=[f"posts/{i:02d}.txt" for i in range(1, 6)],
    )
    (tmp_path / "posts/03.txt").write_bytes(b"caf\xe9 au lait")  # cp1252, not UTF-8
    (tmp_path / "posts/04.txt").mkdir()
    piece, blocking, _ = read(tmp_path)
    assert piece.posts == ["one"]
    assert blocking[0] == "post 2 file posts/02.txt is empty"
    assert blocking[1].startswith("post 3 file posts/03.txt cannot be read as UTF-8 text (")
    assert blocking[2:] == [
        "post 4 file posts/04.txt does not exist",
        "post 5 file posts/05.txt does not exist",
    ]


@pytest.mark.parametrize("posts", [None, [], "posts/01.txt", {"1": "posts/01.txt"}])
def test_posts_must_be_a_list_of_files(tmp_path, posts):
    folder(tmp_path, texts=("one",), posts=posts)
    piece, blocking, _ = read(tmp_path)
    assert blocking == ["piece.json: posts must list the post files in order"]
    assert piece.posts == []


@pytest.mark.parametrize("shape", ["", "carousel", "Long_post", "threads"])
def test_the_shape_must_be_one_of_the_three(tmp_path, shape):
    folder(tmp_path, shape=shape)
    _, blocking, _ = read(tmp_path)
    assert blocking == ["piece.json: shape must be one of long_post, thread, short_post"]


@pytest.mark.parametrize("shape", ["long_post", "thread", "short_post", " thread\n"])
def test_each_shape_reads_cleanly(tmp_path, shape):
    folder(tmp_path, shape=shape)
    piece, blocking, minor = read(tmp_path)
    assert (piece.shape, blocking, minor) == (shape.strip(), [], [])


def test_an_unknown_hook_style_is_fixable_and_none_is_fine(tmp_path):
    folder(tmp_path, hook_style="clickbait")
    _, blocking, minor = read(tmp_path)
    assert blocking == []
    assert minor == ["piece.json: hook_style must be one of " + ", ".join(qa.HOOK_STYLES)]
    folder(tmp_path, hook_style="")
    assert read(tmp_path)[1:] == ([], [])


def _angle_problem(given: str) -> str:
    keys = ", ".join(sorted(load_angles()))
    return (
        "piece.json: angle must be the key of one of the library's angles "
        f"(one of {keys}), not {given!r}"
    )


@pytest.mark.parametrize("angle", ["race_timeline", "", "Deal decoder"])
def test_an_angle_the_library_does_not_know_is_fixable(tmp_path, angle):
    # A card type ("race_timeline"), a missing angle or the display name instead of the key:
    # the account's results are read by angle, so it has to be one of the keys.
    folder(tmp_path, angle=angle)
    _, blocking, minor = read(tmp_path)
    assert (blocking, minor) == ([], [_angle_problem(angle)])


def test_every_angle_in_the_library_reads_cleanly(tmp_path):
    for key in load_angles():
        folder(tmp_path, angle=key)
        assert read(tmp_path)[1:] == ([], []), key


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        ("THE DEAL\n---\nMerck pays $400 million.", True),
        ("THE DEAL\n  ---  \nMerck pays.", True),
        ("THE DEAL\n\nMerck pays.\n\nNot investment advice.", False),
        ("A range: 2031---2033 is not a divider.", False),
        ("THE DEAL\n----\nfour dashes do not split", False),
    ],
)
def test_a_line_of_only_three_dashes_is_fixable(tmp_path, text, flagged):
    folder(tmp_path, texts=(text,))
    report = qa.check_piece(tmp_path, XCFG, set(), None)
    hits = [p for p in report.fixable if "has a line that is only '---'" in p]
    assert bool(hits) is flagged


def test_the_angle_the_editor_chose_is_the_one_the_piece_must_use(tmp_path):
    folder(tmp_path)  # deal_decoder
    asked = qa.check_piece(tmp_path, XCFG, set(), None, requested_angle="catalyst_map")
    assert asked.fixable[-1] == (
        "piece.json: the editor chose the angle 'catalyst_map' for this piece; write it in "
        "that angle (piece.json says 'deal_decoder')"
    )
    kept = qa.check_piece(tmp_path, XCFG, set(), None, requested_angle="deal_decoder")
    assert not any("the editor chose" in p for p in kept.fixable)


def test_an_unreadable_angle_library_skips_the_angle_check(tmp_path, monkeypatch):
    def broken():
        raise ValueError("angles.yaml is not a mapping")

    monkeypatch.setattr(qa, "load_angles", broken)
    folder(tmp_path, angle="anything")
    assert read(tmp_path)[1:] == ([], [])


def test_cards_are_read_with_their_post_alt_type_and_picture_path(tmp_path):
    folder(
        tmp_path,
        texts=("one", "two"),
        title="t" * 250,
        cards=[
            {"file": "cards/card_1.html", "post": 2, "type": "race_timeline", "alt": "x" * 1200},
            {"file": "cards/Card_2.HTM", "alt": "  Phase 3 readouts by quarter  "},
        ],
    )
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards/card_1.html").write_text("<p>a</p>", "utf-8")
    (tmp_path / "cards/Card_2.HTM").write_text("<p>b</p>", "utf-8")
    piece, blocking, minor = read(tmp_path)
    assert (blocking, minor) == ([], [])
    first, second = piece.cards
    assert first.html == (tmp_path / "cards/card_1.html").resolve()
    assert first.png == (tmp_path / "cards/card_1.png").resolve()
    assert (first.post, first.kind, len(first.alt)) == (2, "race_timeline", 1000)
    assert (second.post, second.kind, second.alt) == (1, "", "Phase 3 readouts by quarter")
    assert second.png.name == "Card_2.png"
    assert len(piece.title) == 200


NOT_HTML = "piece.json: card 1 file must be an .html file in your folder"


@pytest.mark.parametrize(
    "card, problem",
    [
        ("card_1.html", "piece.json: card 1 must be an object"),
        ({"file": "cards/card_1.png"}, NOT_HTML),
        ({"file": "../card_1.html"}, NOT_HTML),  # that file exists, outside the folder
        ({"alt": "no file"}, NOT_HTML),
        ({"file": "cards/card_9.html"}, "card 1 file cards/card_9.html does not exist"),
    ],
)
def test_a_card_that_cannot_be_used_is_fixable_and_left_out(tmp_path, card, problem):
    (tmp_path / "card_1.html").write_text("<p>outside</p>", "utf-8")
    work = folder(tmp_path / "piece", card_files=1, cards=[card])
    piece, blocking, minor = read(work)
    assert (blocking, minor, piece.cards) == ([], [problem], [])


def test_cards_that_are_not_a_list_are_fixable(tmp_path):
    folder(tmp_path, card_files=1, cards=CARD)
    piece, blocking, minor = read(tmp_path)
    assert (blocking, minor, piece.cards) == ([], ["piece.json: cards must be a list"], [])


@pytest.mark.parametrize(
    "value, post",
    [
        ("missing", 1),
        (None, 1),
        ("", 1),
        ("2", 2),
        (3, 3),
        (2.0, 2),
        (0, 0),  # kept, so the check names it instead of quietly moving the card
        (-1, -1),
        ("first", 1),
        ([1], 1),
    ],
)
def test_a_cards_post_number(tmp_path, value, post):
    entry = {k: v for k, v in CARD.items() if k != "post"}
    if value != "missing":
        entry["post"] = value
    folder(tmp_path, card_files=1, cards=[entry])
    [card] = read(tmp_path)[0].cards
    assert card.post == post


def test_handles_are_keyed_without_the_at_sign_in_lower_case(tmp_path):
    folder(
        tmp_path,
        handles=[
            {"handle": "@Merck", "verified_at": " https://www.merck.com/ "},
            {"handle": "FDA_Drug_Info"},
            {"handle": "", "verified_at": "https://example.org/"},
            "@NotAnObject",
        ],
    )
    piece = read(tmp_path)[0]
    assert piece.handles == {"merck": "https://www.merck.com/", "fda_drug_info": ""}


@pytest.mark.parametrize(
    "field, value, read_as",
    [
        ("handles", 5, {}),
        ("handles", "@Merck", {}),
        ("companies", 3, []),
        ("companies", "Merck", []),
        ("companies", [{"name": "Merck"}, "MRK"], [{"name": "Merck"}]),
        ("recheck_before_posting", 7, []),
        ("recheck_before_posting", {"what": "the ORR"}, []),
        ("recheck_before_posting", "the ORR on the poster", ["the ORR on the poster"]),
        ("recheck_before_posting", ["the ORR", "", "  ", 12], ["the ORR", "12"]),
    ],
)
def test_list_fields_of_the_wrong_type_never_crash_the_check(tmp_path, field, value, read_as):
    folder(tmp_path, **{field: value})
    piece, blocking, minor = read(tmp_path)
    assert (blocking, minor) == ([], [])
    got = {"handles": piece.handles, "companies": piece.companies}.get(field, piece.recheck)
    assert got == read_as


@pytest.mark.parametrize(
    "value, read_as",
    [
        ("Stifel $38", []),
        ({"firm": "Stifel"}, [{"firm": "Stifel"}]),  # one object is read, and named
        ([{"firm": "Stifel"}, "Stifel $38", 4], [{"firm": "Stifel"}]),
    ],
)
def test_price_targets_of_the_wrong_shape_are_named_not_dropped(tmp_path, value, read_as):
    # Dropped silently, the session would be told it "lists no price_targets"
    folder(tmp_path, price_targets=value)
    piece, blocking, minor = read(tmp_path)
    assert piece.price_targets == read_as
    assert blocking == []
    [problem] = minor
    assert problem.startswith("piece.json: price_targets must be a list of objects")
    assert "post_says" in problem


@pytest.mark.parametrize("value", [None, [], ""])
def test_no_price_targets_is_fine(tmp_path, value):
    folder(tmp_path, price_targets=value)
    piece, blocking, minor = read(tmp_path)
    assert (piece.price_targets, blocking, minor) == ([], [], [])


# ---- check_text ----------------------------------------------------------------------------


def piece_of(*posts: str, shape: str = "", **fields) -> qa.PieceFiles:
    shape = shape or ("long_post" if len(posts) == 1 else "thread")
    return qa.PieceFiles(title="A title", shape=shape, posts=list(posts), **fields)


def a_card(post: int = 1, alt: str = "A chart", name: str = "card_1") -> qa.Card:
    return qa.Card(
        html=Path(f"cards/{name}.html"), png=Path(f"cards/{name}.png"), post=post, alt=alt
    )


def test_a_clean_piece_has_nothing_to_report():
    report = qa.check_text(piece_of(POST, cards=[a_card()]), XCFG, set())
    assert (report.blocking, report.fixable, report.warnings) == ([], [], [])
    assert report.clean and report.problems == []


def test_a_piece_with_no_posts_blocks_and_nothing_else_is_checked():
    report = qa.check_text(
        qa.PieceFiles(shape="thread", cards=[a_card(post=9, alt="")]), XCFG, set()
    )
    assert report.blocking == ["the piece has no posts"]
    assert report.fixable == [] and report.warnings == []


@pytest.mark.parametrize(
    "shape, count, problem",
    [
        ("long_post", 2, "a long_post is exactly one post; you listed 2"),
        ("short_post", 3, "a short_post is exactly one post; you listed 3"),
        ("thread", 1, "a thread needs at least two posts"),
        ("thread", 2, None),
        ("long_post", 1, None),
        ("short_post", 1, None),
    ],
)
def test_the_number_of_posts_must_fit_the_shape(shape, count, problem):
    posts = [f"Post {i}." for i in range(1, count)] + [POST]
    report = qa.check_text(piece_of(*posts, shape=shape), XCFG, set())
    assert report.blocking == ([problem] if problem else [])


def test_a_post_may_run_to_its_limit_less_the_headroom():
    xcfg = {**XCFG, "long_post_max": 300, "headroom": 50}
    fits = CLOSING + " " + "x" * (250 - len(CLOSING) - 1)
    assert qa.check_text(piece_of(fits), xcfg, set()).blocking == []
    report = qa.check_text(piece_of(fits + "x"), xcfg, set())
    assert report.blocking == ["post 1 is 251 characters as X counts them; keep it under 250"]


def test_the_limit_counts_characters_the_way_x_does():
    xcfg = {**XCFG, "long_post_max": 300, "headroom": 50}
    text = "免疫" * 63 + CLOSING  # 126 CJK characters weigh 2 each: 252 + 22
    assert len(text) < 250
    [problem] = qa.check_text(piece_of(text), xcfg, set()).blocking
    assert problem.startswith("post 1 is 274 characters as X counts them")


def test_a_thread_is_held_to_the_thread_limit_and_the_rest_to_the_long_limit():
    xcfg = {**XCFG, "thread_post_max": 120, "long_post_max": 1000, "headroom": 0}
    long_one = "Immunotherapy " * 10 + CLOSING  # 162 characters
    thread = qa.check_text(piece_of("Opening.", long_one), xcfg, set())
    assert thread.blocking == ["post 2 is 162 characters as X counts them; keep it under 120"]
    for shape in ("long_post", "short_post"):
        assert qa.check_text(piece_of(long_one, shape=shape), xcfg, set()).blocking == []


def test_a_short_post_over_its_target_is_a_warning_not_a_block():
    xcfg = {**XCFG, "short_post_max": 100}
    text = "Merck's deal " * 10 + CLOSING
    report = qa.check_text(piece_of(text, shape="short_post"), xcfg, set())
    assert report.blocking == []
    assert report.warnings == ["the short post runs 152 characters (target under 100)"]
    assert qa.check_text(piece_of(text, shape="long_post"), xcfg, set()).warnings == []
    no_target = {**XCFG, "short_post_max": 0}
    assert qa.check_text(piece_of(text, shape="short_post"), no_target, set()).warnings == []


def test_safety_problems_block_and_name_the_post():
    posts = (
        "The data are clean; see clinicaltrials.gov for the protocol.",
        "Investors should buy the stock before the readout.",
        "9926.HK trades at a discount. " + CLOSING,
    )
    report = qa.check_text(piece_of(*posts), XCFG, set())
    assert report.blocking == [
        *safety.blocking_problems(posts[0], "post 1"),
        *safety.blocking_problems(posts[1], "post 2"),
    ]
    assert [p.split(" (")[0] for p in report.blocking] == [
        "post 1 contains a link",
        "post 2 reads as investment advice",
    ]


def test_an_unverified_handle_is_fixable_not_blocking():
    text = "@Merck and @FDA signed off. @Genmab did not. " + CLOSING
    pages = {"merck": "https://www.merck.com/", "fda": "fda.gov", "genmab": ""}
    report = qa.check_text(piece_of(text, handles=pages), XCFG, set())
    assert report.blocking == []
    assert [p.split(" but ")[0] for p in report.fixable] == [
        "post 1 tags @FDA",
        "post 1 tags @Genmab",
    ]
    assert report.fixable[0].endswith(
        "gives no page where you verified it; verify it on the organisation's own site or "
        "write the name without the @"
    )


def test_a_known_handle_needs_no_page_whatever_its_case():
    text = "@MERCK and @merck and @Merck. " + CLOSING
    assert qa.check_text(piece_of(text), XCFG, {"merck"}).fixable == []
    assert len(qa.check_text(piece_of(text), XCFG, set()).fixable) == 3


def test_a_handle_verified_on_a_page_is_fine_in_any_case():
    text = "@MERCK paid up. " + CLOSING
    for page in ("https://www.merck.com/", "http://www.merck.com/news"):
        report = qa.check_text(piece_of(text, handles={"merck": page}), XCFG, set())
        assert report.fixable == []


def test_more_cards_than_the_piece_allows_blocks():
    cards = [a_card(post=1 + i % 2, name=f"card_{i}") for i in range(5)]
    report = qa.check_text(piece_of("One.", POST, cards=cards), XCFG, set())
    assert report.blocking == ["5 cards; at most 4 per piece"]


def test_x_allows_four_pictures_per_post():
    xcfg = {**XCFG, "max_cards_total": 8}
    four = [a_card(name=f"card_{i}") for i in range(4)]
    assert qa.check_text(piece_of(POST, cards=four), xcfg, set()).blocking == []
    five = [*four, a_card(name="card_5")]
    report = qa.check_text(piece_of(POST, cards=five), xcfg, set())
    assert report.blocking == ["post 1 carries 5 cards; X allows 4 per post"]


def test_a_card_on_a_post_that_does_not_exist_or_without_alt_is_fixable():
    cards = [
        a_card(post=3, name="card_1"),
        a_card(post=0, name="card_2"),
        a_card(alt="", name="card_3"),
    ]
    report = qa.check_text(piece_of("One.", POST, cards=cards), XCFG, set())
    assert report.blocking == []
    assert report.fixable == [
        "card card_1.html is attached to post 3, which does not exist",
        "card card_2.html is attached to post 0, which does not exist",
        "card card_3.html has no alt text in piece.json",
    ]


def test_a_piece_without_a_title_is_fixable():
    report = qa.check_text(qa.PieceFiles(shape="long_post", posts=[POST]), XCFG, set())
    assert report.fixable == ["piece.json has no title"]


def test_the_editor_is_warned_about_a_missing_disclaimer():
    report = qa.check_text(piece_of("Phase 3 reads out in May.", "Then the label."), XCFG, set())
    assert (report.blocking, report.fixable) == ([], [])
    assert report.warnings == ['the last post has no "Not investment advice." line']


# ---- analysts' price targets -------------------------------------------------------------------

# The smoke test's Iovance piece, as written before targets had to say what they rest on.
IOVANCE = (
    "The stock closed at $14.45, up 31.5%. H.C. Wainwright raised its target to $20 from $9, "
    "and Wells Fargo to $18 from $14 the next day; Goldman had put a Buy and a $15 target on "
    "it five days earlier. At Friday's close of $14.22 the company is worth about $6.4B, and "
    "the shares sit above the average analyst target (~$12.40 on MarketBeat's tally). " + CLOSING
)
# The same moves the way the voice guide asks: what each rests on, and whether the NSCLC
# readout the piece is about is in it.
IOVANCE_EXPLAINED = (
    "H.C. Wainwright raised its target to $20 from $9, and Wells Fargo to $18 from $14; both "
    "value Amtagvi in melanoma alone, so the NSCLC readout is not in either. Goldman's $15 "
    "target, from five days earlier, gives NSCLC a 30% chance. The average analyst target "
    "(~$12.40 on MarketBeat's tally) mixes models written before the label expansion. " + CLOSING
)
BOTH = "both value Amtagvi in melanoma alone, so the NSCLC readout is not in either"


def target(firm: str, figure: str, **fields) -> dict:
    """A complete price_targets entry: the firm's figure, what it rests on, the catalyst,
    and the post's words that say so."""
    return {
        "firm": firm,
        "target": figure,
        "date": "2026-09-30",
        "rests_on": "Amtagvi melanoma sales only, peak $1B, no NSCLC",
        "catalyst": "the NSCLC registrational readout",
        "in_model": "no",
        "effect": "adds an indication the model values at zero; up",
        "post_says": "values melanoma alone",
        "source": "https://www.marketbeat.com/stocks/NASDAQ/IOVA/price-target/",
        **fields,
    }


IOVANCE_TARGETS = [
    target("H.C. Wainwright", "$20", previous="$9", post_says=BOTH),
    target("Wells Fargo", "$18", previous="$14", post_says=BOTH),
    target(
        "Goldman Sachs",
        "$15",
        date="2026-09-24",
        in_model="partly",
        post_says="Goldman's $15 target, from five days earlier, gives NSCLC a 30% chance",
    ),
    target(
        "consensus (MarketBeat, 9 analysts)",
        "$12.40",
        in_model="unknown",
        post_says="mixes models written before the label expansion",
    ),
]


def test_a_cited_target_with_nothing_listed_goes_back_to_the_session():
    report = qa.check_text(piece_of(IOVANCE), XCFG, set())
    assert report.blocking == []
    [problem] = report.fixable  # one message, not one per figure as well
    assert problem.startswith(
        "post 1 cites a price target ('target to $20') but piece.json lists no price_targets"
    )
    assert "what each cited target assumes" in problem and "or take the target out" in problem
    assert "in post_says" in problem


def test_targets_the_post_explains_pass_and_the_editor_sees_them():
    report = qa.check_text(piece_of(IOVANCE_EXPLAINED, price_targets=IOVANCE_TARGETS), XCFG, set())
    assert (report.blocking, report.fixable) == ([], [])
    assert report.warnings == [
        "cites analyst targets (H.C. Wainwright $20, Wells Fargo $18, Goldman Sachs $15, "
        "consensus (MarketBeat, 9 analysts) $12.40): check the post says what each rests on "
        "and whether the catalysts it says to watch are in it, and that no target, fair value "
        "or value per share of the account's own appears"
    ]


def test_a_target_the_post_does_not_explain_goes_back_whatever_piece_json_says():
    # piece.json has every field, but the post itself only lists the moves (the editor's
    # complaint): each entry's post_says is in no post
    report = qa.check_text(piece_of(IOVANCE, price_targets=IOVANCE_TARGETS), XCFG, set())
    assert report.blocking == []
    assert [p.split(" post_says")[0] for p in report.fixable] == [
        "piece.json: price_targets[1] (H.C. Wainwright $20)",
        "piece.json: price_targets[2] (Wells Fargo $18)",
        "piece.json: price_targets[3] (Goldman Sachs $15)",
        "piece.json: price_targets[4] (consensus (MarketBeat, 9 analysts) $12.40)",
    ]
    assert "is in no post or card; copy the words of the post" in report.fixable[0]


def test_post_says_is_found_whatever_the_quotes_case_and_spacing():
    text = "Stifel\u2019s $38 target values the  squamous cohort \u2014 and only it. " + CLOSING
    for says in (
        "Stifel's $38 target values the squamous cohort - and only it.",
        '"values the squamous cohort"',
        ["STIFEL\u2019S $38 TARGET", "and only it"],
    ):
        entry = target("Stifel", "$38", post_says=says)
        report = qa.check_text(piece_of(text, price_targets=[entry]), XCFG, set())
        assert report.fixable == [], (says, report.fixable)


def test_post_says_must_be_more_than_one_word():
    text = "Stifel's $38 target values melanoma alone. " + CLOSING
    entry = target("Stifel", "$38", post_says="melanoma")
    [problem] = qa.check_text(piece_of(text, price_targets=[entry]), XCFG, set()).fixable
    assert "post_says is 'melanoma'; quote the whole stretch of the post" in problem


def test_a_figure_given_as_a_target_must_be_a_listed_published_one():
    # Wells Fargo's move is left out of the list: both its figures are named
    listed = [t for t in IOVANCE_TARGETS if t["firm"] != "Wells Fargo"]
    report = qa.check_text(piece_of(IOVANCE_EXPLAINED, price_targets=listed), XCFG, set())
    assert [p.split(",")[0] for p in report.fixable] == [
        "post 1 gives 18 as a target",
        "post 1 gives 14 as a target",
    ]
    assert "the account sets no targets of its own" in report.fixable[0]
    # the same figure written another way is the same target, and a share price set
    # against the target is not one
    for figure in ("38.00 dollars", "$38 (note of 2026-07-24)", 38):
        same = [target("Stifel", figure)]
        text = "Stifel's $38 target vs $14.45 today values melanoma alone. " + CLOSING
        assert qa.check_text(piece_of(text, price_targets=same), XCFG, set()).fixable == []


def test_a_four_figure_or_foreign_target_is_matched_by_its_figure():
    text = (
        "Leerink's $1,200 target on Regeneron values melanoma alone; Citi's HK$130 on Akeso "
        "values melanoma alone too. " + CLOSING
    )
    listed = [target("Leerink", "$1,200"), target("Citi", "HK$130")]
    assert qa.check_text(piece_of(text, price_targets=listed), XCFG, set()).fixable == []
    report = qa.check_text(piece_of(text, price_targets=listed[1:]), XCFG, set())
    assert [p.split(",")[0] for p in report.fixable] == ["post 1 gives 1200 as a target"]


@pytest.mark.parametrize(
    "fields, problem",
    [
        ({"firm": ""}, "price_targets[1] ($20) has no firm"),
        ({"target": "twenty"}, "price_targets[1] (H.C. Wainwright twenty) has no target"),
        ({"date": " "}, "price_targets[1] (H.C. Wainwright $20) has no date"),
        ({"rests_on": ""}, "price_targets[1] (H.C. Wainwright $20) has no rests_on"),
        ({"catalyst": None}, "price_targets[1] (H.C. Wainwright $20) has no catalyst"),
        ({"rests_on": "", "catalyst": ""}, "has no rests_on, catalyst"),
        ({"post_says": ""}, "price_targets[1] (H.C. Wainwright $20) has no post_says"),
        ({"source": "MarketBeat"}, "price_targets[1] (H.C. Wainwright $20) has no source URL"),
        # an empty list or an object is no answer
        ({"rests_on": []}, "price_targets[1] (H.C. Wainwright $20) has no rests_on"),
        ({"catalyst": [""]}, "price_targets[1] (H.C. Wainwright $20) has no catalyst"),
        ({"date": {}}, "price_targets[1] (H.C. Wainwright $20) has no date"),
        ({"in_model": "no", "effect": []}, "says the catalyst is 'no' in the model but gives no"),
    ],
)
def test_each_listed_target_says_whose_it_is_what_it_rests_on_and_where_from(fields, problem):
    text = "H.C. Wainwright's $20 target values melanoma alone. " + CLOSING
    entry = {**target("H.C. Wainwright", "$20"), **fields}
    report = qa.check_text(piece_of(text, price_targets=[entry]), XCFG, set())
    assert [p for p in report.fixable if problem in p], report.fixable


def test_list_fields_of_an_entry_are_read_as_lists():
    text = "H.C. Wainwright's $20 target values melanoma alone. " + CLOSING
    entry = target(
        "H.C. Wainwright",
        "$20",
        rests_on=["melanoma only", "peak $1B"],
        source=["MarketBeat", "https://www.marketbeat.com/stocks/NASDAQ/IOVA/price-target/"],
    )
    assert qa.check_text(piece_of(text, price_targets=[entry]), XCFG, set()).fixable == []


def test_each_firm_is_listed_once_with_its_previous_target():
    text = "Stifel cut its $45 target to $38; it values melanoma alone. " + CLOSING
    listed = [target("Stifel", "$38"), target("stifel", "$45", date="2026-07-01")]
    [problem] = qa.check_text(piece_of(text, price_targets=listed), XCFG, set()).fixable
    assert problem.startswith("piece.json: price_targets lists 'stifel' 2 times")
    assert "with the one before it in previous" in problem


@pytest.mark.parametrize(
    "in_model, effect, problem",
    [
        ("yes", "", None),
        ("no", "adds NSCLC; up", None),
        ("Partly", "raises the odds", None),
        ("unknown", "", "says the catalyst is 'unknown' in the model but gives no effect"),
        ("no", "", "says the catalyst is 'no' in the model but gives no effect"),
        ("maybe", "", "in_model must be one of yes, no, partly, unknown"),
        ("", "", "in_model must be one of yes, no, partly, unknown"),
        (True, "", "in_model must be one of yes, no, partly, unknown"),
    ],
)
def test_whether_the_catalyst_is_in_the_model_and_what_it_would_move(in_model, effect, problem):
    text = "H.C. Wainwright's $20 target values melanoma alone. " + CLOSING
    entry = target("H.C. Wainwright", "$20", in_model=in_model, effect=effect)
    fixable = qa.check_text(piece_of(text, price_targets=[entry]), XCFG, set()).fixable
    assert fixable == [] if problem is None else [p for p in fixable if problem in p], fixable


def test_an_entry_no_post_cites_any_more_is_named_and_left_out_of_the_editors_note():
    # The editor cut Jefferies from the post in the queue; the entry is still in piece.json
    text = "H.C. Wainwright's $20 target values melanoma alone. " + CLOSING
    cut = "Jefferies' $40 target values the lung label too"
    listed = [target("H.C. Wainwright", "$20"), target("Jefferies", "$40", post_says=cut)]
    report = qa.check_text(piece_of(text, price_targets=listed), XCFG, set())
    [problem] = report.fixable
    assert problem.startswith("piece.json: price_targets[2] (Jefferies $40) post_says")
    assert "if no post cites this target any more, remove the entry" in problem
    assert report.warnings[-1].startswith("cites analyst targets (H.C. Wainwright $20):")
    assert [e["firm"] for e in qa.cited_targets(piece_of(text, price_targets=listed))] == [
        "H.C. Wainwright"
    ]


def test_a_target_on_a_card_counts_too(tmp_path):
    card = tmp_path / "card_1.html"
    card.write_text(
        "<html><head><title>Analyst price targets</title><style>.t{color:red}</style>"
        "</head><body><h1>Street targets</h1><!-- Stifel $52 PT, cut -->"
        "<p>Average target&nbsp;<b>$12.40</b></p><p>H.C. Wainwright: $20 PT</p>"
        "<p>Both leave NSCLC out of the model</p></body></html>",
        encoding="utf-8",
    )
    cards = [qa.Card(html=card, png=card.with_suffix(".png"), post=1, alt="Street targets")]
    report = qa.check_text(piece_of(POST, cards=cards), XCFG, set())
    assert report.fixable[0].startswith("card card_1.html cites a price target ('Street targets')")
    says = "both leave NSCLC out of the model"
    listed = [
        target("consensus (MarketBeat)", "$12.40", post_says=says),
        target("H.C. Wainwright", "$20", post_says=says),
    ]
    report = qa.check_text(piece_of(POST, cards=cards, price_targets=listed), XCFG, set())
    assert report.fixable == []  # the comment's $52 is not on the card a reader sees
    assert qa.card_text(tmp_path / "gone.html") == ""


def test_card_text_is_what_a_reader_sees(tmp_path):
    card = tmp_path / "card.html"
    card.write_text(
        "<html><head><title>Analyst price targets</title><script>var t='$52 PT'</script>"
        "</head><body><p>ORR 31% (p<0.001) and Stifel's $52 target</p><!-- $60 PT -->"
        "<p>caf&eacute; &amp; &#36;12</p></body></html>",
        encoding="utf-8",
    )
    assert qa.card_text(card) == "ORR 31% (p<0.001) and Stifel's $52 target caf\u00e9 & $12"


def test_a_target_of_the_accounts_own_on_a_card_blocks(tmp_path):
    card = tmp_path / "card_1.html"
    card.write_text("<div>Our price target: $52</div>", encoding="utf-8")
    cards = [qa.Card(html=card, png=card.with_suffix(".png"), post=1, alt="A target")]
    report = qa.check_text(piece_of(POST, cards=cards), XCFG, set())
    [problem] = report.blocking
    assert problem.startswith(
        "card card_1.html gives a price target of the account's own ('Our price target')"
    )


@pytest.mark.parametrize(
    "html, alt, problem",
    [
        ("<p>Buy the stock before the readout</p>", "A chart", "reads as investment advice"),
        ("<p>Ask your doctor about ivonescimab</p>", "A chart", "reads as medical advice"),
        ("<p>Bull case</p>", "Bull case: the stock is worth $60", "alt text gives a price target"),
    ],
)
def test_a_card_and_its_alt_text_are_held_to_the_posts_lines(tmp_path, html, alt, problem):
    card = tmp_path / "card_1.html"
    card.write_text(html, encoding="utf-8")
    cards = [qa.Card(html=card, png=card.with_suffix(".png"), post=1, alt=alt)]
    [found] = qa.check_text(piece_of(POST, cards=cards), XCFG, set()).blocking
    assert found.startswith("card card_1.html") and problem in found, found


def test_a_card_may_name_a_site_without_it_being_a_link(tmp_path):
    card = tmp_path / "card_1.html"
    card.write_text("<p>ORR 31%</p><p>Source: iovance.com</p>", encoding="utf-8")
    cards = [qa.Card(html=card, png=card.with_suffix(".png"), post=1, alt="ORR")]
    assert qa.check_text(piece_of(POST, cards=cards), XCFG, set()).blocking == []


@pytest.mark.parametrize(
    "case",
    ["its bull-case target of $60 assumes an NSCLC win", "its bull case, $60, assumes a win"],
)
def test_a_firms_published_cases_are_its_figures_not_the_accounts(case):
    text = f"Stifel's $38 target values melanoma alone, and {case}. " + CLOSING
    entry = target("Stifel", "$38", cases="bull $60, bear $20 (published 2026-07-24)")
    assert qa.check_text(piece_of(text, price_targets=[entry]), XCFG, set()).fixable == []
    del entry["cases"]
    [problem] = qa.check_text(piece_of(text, price_targets=[entry]), XCFG, set()).fixable
    assert problem.startswith("post 1 gives 60 as a target")
    assert "or a bull or bear value in cases" in problem


TARGET_TABLE = (
    "<table><tr><th>Firm</th><th>Price target</th><th>Prior</th><th>Rests on</th></tr>"
    "<tr><td>H.C. Wainwright</td><td>$20</td><td>$9</td><td>melanoma alone</td></tr>"
    "<tr><td>Stifel</td><td>$52</td><td>$45</td><td>NSCLC at 35%</td></tr></table>"
)


def test_a_table_of_targets_on_a_card_is_held_to_the_list(tmp_path):
    card = tmp_path / "card_1.html"
    card.write_text("<p>Last close $14.45</p>" + TARGET_TABLE, encoding="utf-8")
    cards = [qa.Card(html=card, png=card.with_suffix(".png"), post=1, alt="Where the Street is")]
    listed = [target("H.C. Wainwright", "$20", previous="$9", post_says="melanoma alone")]
    report = qa.check_text(piece_of(POST, cards=cards, price_targets=listed), XCFG, set())
    # Stifel's row is checked, the share price beside the table is not
    assert [p.split(",")[0] for p in report.fixable] == [
        "card card_1.html gives 52 as a target",
        "card card_1.html gives 45 as a target",
    ]
    listed.append(target("Stifel", "$52", previous="$45", post_says="NSCLC at 35%"))
    report = qa.check_text(piece_of(POST, cards=cards, price_targets=listed), XCFG, set())
    assert report.fixable == []
    assert [e["firm"] for e in qa.cited_targets(report.piece)] == ["H.C. Wainwright", "Stifel"]


def test_a_table_of_targets_with_nothing_listed_goes_back(tmp_path):
    card = tmp_path / "card_1.html"
    card.write_text(
        "<table><tr><td>Firm</td><td>Target</td><td>Rests on</td></tr>"
        "<tr><td>Stifel</td><td>$38</td><td>melanoma alone</td></tr></table>",
        encoding="utf-8",
    )
    cards = [qa.Card(html=card, png=card.with_suffix(".png"), post=1, alt="Ratings")]
    [problem] = qa.check_text(piece_of(POST, cards=cards), XCFG, set()).fixable
    assert problem.startswith("card card_1.html cites a price target ('a table of targets: $38')")


@pytest.mark.parametrize(
    "table",
    [
        # a deal's target is the company bought; a pipeline's is an antigen
        "<table><tr><th>Target</th><th>Acquirer</th><th>Prior close</th><th>Per share</th></tr>"
        "<tr><td>Verona</td><td>Merck</td><td>$80.50</td><td>$107</td></tr></table>",
        "<table><tr><th>Asset</th><th>Target</th><th>Stage</th></tr>"
        "<tr><td>anito-cel</td><td>BCMA</td><td>Ph3</td></tr></table>",
    ],
)
def test_other_tables_with_a_target_column_are_not_targets(tmp_path, table):
    card = tmp_path / "card_1.html"
    card.write_text(table, encoding="utf-8")
    cards = [qa.Card(html=card, png=card.with_suffix(".png"), post=1, alt="A table")]
    assert qa.card_table_targets(card) == []
    assert qa.check_text(piece_of(POST, cards=cards), XCFG, set()).fixable == []


def test_biotech_targets_are_not_price_targets():
    text = (
        "Ivonescimab targets PD-1 and VEGF; the target population is 780 patients and the "
        "company is chasing a $5B target market. Median target lesion shrinkage was 38%, and "
        "Merck values the target at $46 a share in cash. " + CLOSING
    )
    report = qa.check_text(piece_of(text), XCFG, set())
    assert (report.blocking, report.fixable, report.warnings) == ([], [], [])


# ---- check_side_files ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "factbase, factcheck, fixable, blocking",
    [
        ("- a fact", "| a check |", [], []),
        (None, "| a check |", ["factbase.md is missing or empty"], []),
        ("- a fact", " \n\n ", [], [qa.NO_FACTCHECK]),
        ("dir", None, ["factbase.md is missing or empty"], [qa.NO_FACTCHECK]),
    ],
)
def test_the_fact_base_and_the_fact_check_log_must_have_content(
    tmp_path, factbase, factcheck, fixable, blocking
):
    for name, text in (("factbase.md", factbase), ("factcheck.md", factcheck)):
        if text == "dir":
            (tmp_path / name).mkdir()
        elif text is not None:
            (tmp_path / name).write_text(text, encoding="utf-8")
    report = qa.Report()
    qa.check_side_files(tmp_path, report)
    assert report.fixable == fixable and report.blocking == blocking


def test_a_piece_without_its_cold_fact_check_never_reaches_the_queue_unchecked():
    """The fact-check is what makes a piece postable: without its log the piece blocks
    (the polish rounds ask the session for it first), and the message says how."""
    assert qa.NO_FACTCHECK.startswith("factcheck.md is missing or empty: run the cold fact-check")
    assert "wait for its report" in qa.NO_FACTCHECK


# ---- check_cards ---------------------------------------------------------------------------


class Renderer:
    """Stands in for render.render_card: writes a PNG header and reports as told, per card
    (keyed by the card's file stem: png=(w, h) or "junk", size=, problems=, fail=)."""

    def __init__(self, **per_card):
        self.per_card = per_card
        self.calls: list[tuple[Path, Path]] = []

    def __call__(self, html: Path, png: Path) -> render.RenderResult:
        self.calls.append((html, png))
        how = self.per_card.get(html.stem, {})
        if "fail" in how:
            raise render.RenderError(how["fail"])
        size = how.get("size", render.DEFAULT_SIZE)
        pixels = how.get("png", qa.PNG_SIZES[size])
        png.write_bytes(b"GIF89a, not a PNG" if pixels == "junk" else png_header(*pixels))
        return render.RenderResult(png=png, size=size, problems=list(how.get("problems", [])))


def drawn_piece(tmp_path: Path, count: int) -> qa.PieceFiles:
    folder(tmp_path, card_files=count)
    return read(tmp_path)[0]


def test_the_expected_pictures_are_the_house_sizes_at_twice_the_scale():
    assert qa.PNG_SIZES == {(1080, 1350): (2160, 2700), (1600, 900): (3200, 1800)}
    for (w, h), pixels in qa.PNG_SIZES.items():
        assert pixels == (w * render.SCALE, h * render.SCALE)


def test_a_piece_without_cards_needs_no_browser(tmp_path):
    piece = drawn_piece(tmp_path, 0)
    stand_in = Renderer()
    for renderer in (None, stand_in):
        report = qa.Report()
        qa.check_cards(piece, report, renderer)
        assert report == qa.Report()
    assert stand_in.calls == []


def test_cards_without_a_browser_block(tmp_path):
    report = qa.Report()
    qa.check_cards(drawn_piece(tmp_path, 1), report, None)
    assert report.blocking == [
        "no browser to draw the cards with (see render.browser in studio/config.yaml)"
    ]
    assert report.pictures == []


def test_every_card_is_drawn_next_to_its_html_and_listed_for_the_session(tmp_path):
    piece = drawn_piece(tmp_path, 3)
    renderer = Renderer()
    report = qa.Report()
    qa.check_cards(piece, report, renderer)
    assert report.clean and report.blocking == []
    assert renderer.calls == [(c.html, c.png) for c in piece.cards]
    assert report.pictures == [str(tmp_path.resolve() / f"cards/card_{i}.png") for i in (1, 2, 3)]


def test_a_card_that_cannot_be_drawn_blocks_and_the_rest_are_still_drawn(tmp_path):
    renderer = Renderer(card_2={"fail": "the browser took longer than 60s"})
    report = qa.Report()
    qa.check_cards(drawn_piece(tmp_path, 3), report, renderer)
    assert report.blocking == [
        "card card_2.html could not be drawn: the browser took longer than 60s"
    ]
    assert len(renderer.calls) == 3
    assert [Path(p).name for p in report.pictures] == ["card_1.png", "card_3.png"]


@pytest.mark.parametrize(
    "how, came_out",
    [
        ({"png": (1080, 1350)}, "(1080, 1350) instead of (2160, 2700)"),
        ({"png": (3200, 1800)}, "(3200, 1800) instead of (2160, 2700)"),
        ({"png": (2160, 2700), "size": render.WIDE_SIZE}, "(2160, 2700) instead of (3200, 1800)"),
        ({"png": "junk"}, "None instead of (2160, 2700)"),
    ],
)
def test_a_picture_of_the_wrong_size_blocks(tmp_path, how, came_out):
    report = qa.Report()
    qa.check_cards(drawn_piece(tmp_path, 1), report, Renderer(card_1=how))
    assert report.blocking == [f"card card_1.html came out {came_out}; size the page to the card"]
    assert len(report.pictures) == 1  # the session still gets to look at it


def test_a_wide_card_at_its_own_size_is_fine(tmp_path):
    report = qa.Report()
    qa.check_cards(drawn_piece(tmp_path, 1), report, Renderer(card_1={"size": render.WIDE_SIZE}))
    assert report.clean and len(report.pictures) == 1


def test_layout_problems_are_fixable_and_name_their_card(tmp_path):
    renderer = Renderer(
        card_1={"problems": ['text overlaps other text: "a" and "b"']},
        card_2={"problems": ["the page is larger than the 1080x1350 card (1080x1400)"]},
    )
    report = qa.Report()
    qa.check_cards(drawn_piece(tmp_path, 2), report, renderer)
    assert report.blocking == []
    assert report.fixable == [
        'card card_1.html: text overlaps other text: "a" and "b"',
        "card card_2.html: the page is larger than the 1080x1350 card (1080x1400)",
    ]
    assert len(report.pictures) == 2


# ---- check_piece ---------------------------------------------------------------------------


def test_a_finished_piece_is_clean_end_to_end(tmp_path):
    folder(tmp_path, texts=("Opening post.", POST), card_files=1)
    renderer = Renderer()
    report = qa.check_piece(tmp_path, XCFG, set(), renderer)
    assert (report.blocking, report.fixable, report.warnings) == ([], [], [])
    assert report.clean
    assert report.pictures == [str((tmp_path / "cards/card_1.png").resolve())]
    assert report.piece.shape == "thread" and report.piece.posts == ["Opening post.", POST]
    assert report.piece.companies == [{"name": "Merck", "ticker": "MRK"}]


def test_without_piece_json_only_that_is_reported(tmp_path):
    renderer = Renderer()
    report = qa.check_piece(tmp_path, XCFG, set(), renderer)
    assert report.blocking == ["piece.json is missing; write it as the write stage describes"]
    assert (report.fixable, report.warnings, report.pictures, report.piece) == ([], [], [], None)
    assert renderer.calls == []


def test_every_kind_of_problem_lands_where_the_polish_loop_expects_it(tmp_path):
    advice = "Investors should buy the stock now."
    folder(
        tmp_path,
        texts=(advice, "@Genmab reports in May."),
        card_files=2,
        side_files=False,
        title="",
        hook_style="clickbait",
        posts=["posts/01.txt", "posts/02.txt", "posts/09.txt"],
        cards=[CARD, {**CARD, "file": "cards/card_2.html", "post": 5}],
    )
    renderer = Renderer(card_1={"problems": ["text runs off the card"]}, card_2={"png": (10, 10)})
    report = qa.check_piece(tmp_path, XCFG, {"merck"}, renderer)
    assert report.blocking == [
        *safety.blocking_problems(advice, "post 1"),
        "post 3 file posts/09.txt does not exist",
        qa.NO_FACTCHECK,
        "card card_2.html came out (10, 10) instead of (2160, 2700); size the page to the card",
    ]
    assert report.fixable == [
        "post 2 tags @Genmab but piece.json gives no page where you verified it; verify it on "
        "the organisation's own site or write the name without the @",
        "card card_2.html is attached to post 5, which does not exist",
        "piece.json has no title",
        "piece.json: hook_style must be one of " + ", ".join(qa.HOOK_STYLES),
        "factbase.md is missing or empty",
        "card card_1.html: text runs off the card",
    ]
    assert report.warnings == ['the last post has no "Not investment advice." line']
    assert report.problems == report.blocking + report.fixable and not report.clean


@functools.cache
def _browser() -> str | None:
    from tests.test_studio_render import working_browser

    return working_browser()


def test_a_piece_checked_with_the_real_renderer(tmp_path):
    browser = _browser()
    if browser is None:
        pytest.skip("no Chromium-family browser that can draw here")
    folder(tmp_path, card_files=2)
    style = "<style>html,body{margin:0;width:1080px;height:1350px;font-family:Inter}</style>"
    # A thin rail down the right side keeps the empty-band check quiet about these sparse cards.
    rail = (
        '<i style="position:absolute;right:30px;top:30px;width:2px;height:1290px;'
        'background:#334"></i>'
    )
    (tmp_path / "cards/card_1.html").write_text(
        f"<!doctype html><html><head>{style}</head><body>"
        '<h1 style="margin:0;padding:60px;font-size:52px">Merck pays $400 million</h1>'
        f"{rail}</body></html>",
        encoding="utf-8",
    )
    (tmp_path / "cards/card_2.html").write_text(
        f"<!doctype html><html><head>{style}</head><body>"
        '<p style="position:absolute;left:4px;top:600px;margin:0">At the edge</p>'
        f"{rail}</body></html>",
        encoding="utf-8",
    )
    renderer = functools.partial(render.render_card, browser=browser)
    report = qa.check_piece(tmp_path, XCFG, set(), renderer)
    assert report.blocking == []
    assert report.fixable == [
        'card card_2.html: text runs off the card or within 20px of its edge: "At the edge"'
    ]
    assert len(report.pictures) == 2
    for picture in report.pictures:
        assert render.png_size(Path(picture)) == (2160, 2700)
