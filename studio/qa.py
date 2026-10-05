"""The app's check of a finished piece: read the session's files, draw the cards, count
characters the way X does and run the safety lines. Produces the report the polish
stage hands back to the session, and the Piece the queue receives.

Reading and validating is pure over the workspace folder; drawing cards goes through
studio/render.py (a headless browser, no network).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from studio import render as render_mod
from studio import safety
from studio.angles import HOOK_STYLES, SHAPES, load_angles
from studio.prompt import FACTBASE_FILE, FACTCHECK_FILE, PIECE_FILE
from studio.xcount import x_length

log = logging.getLogger(__name__)

PNG_SIZES = {
    render_mod.DEFAULT_SIZE: (2160, 2700),
    render_mod.WIDE_SIZE: (3200, 1800),
}
NO_BROWSER = "no browser to draw the cards with (see render.browser in studio/config.yaml)"


@dataclass
class Card:
    html: Path
    png: Path
    post: int  # 1-based post the card is attached to
    alt: str = ""
    kind: str = ""


@dataclass
class PieceFiles:
    """What the session produced, as read from its folder."""

    title: str = ""
    angle: str = ""
    angle_reason: str = ""
    shape: str = ""
    hook_style: str = ""
    posts: list[str] = field(default_factory=list)
    post_files: list[str] = field(default_factory=list)
    cards: list[Card] = field(default_factory=list)
    companies: list[dict[str, Any]] = field(default_factory=list)
    handles: dict[str, str] = field(default_factory=dict)  # lowercased handle -> page
    recheck: list[str] = field(default_factory=list)
    summary: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Report:
    blocking: list[str] = field(default_factory=list)  # must be fixed before the queue
    fixable: list[str] = field(
        default_factory=list
    )  # sent back to the session; tolerated after the last round
    warnings: list[str] = field(default_factory=list)  # shown to the editor only
    pictures: list[str] = field(default_factory=list)  # rendered PNGs, for the session to look at
    piece: PieceFiles | None = None

    @property
    def problems(self) -> list[str]:
        return self.blocking + self.fixable

    @property
    def clean(self) -> bool:
        return not self.blocking and not self.fixable


def _str(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _items(value: Any) -> list[Any]:
    """A field that should be a JSON list: one string is one item, anything else none."""
    if isinstance(value, list):
        return value
    return [value] if isinstance(value, str) and value.strip() else []


def _inside(root: Path, rel: str) -> Path | None:
    """`rel` resolved under `root`, or None when it points outside (or is absolute)."""
    if not rel or Path(rel).is_absolute():
        return None
    try:
        path = (root / rel).resolve()
        path.relative_to(root.resolve())
    except (OSError, ValueError):  # outside, or not a path at all (a NUL byte)
        return None
    return path


def read_piece(workspace: Path) -> tuple[PieceFiles | None, list[str], list[str]]:
    """Parse piece.json and the files it names. Returns the piece (None when piece.json
    is unusable), the blocking problems found reading it (a post that cannot be read
    cannot be posted) and the fixable ones."""
    problems: list[str] = []  # blocking
    minor: list[str] = []
    path = workspace / PIECE_FILE
    if not path.is_file():
        return None, [f"{PIECE_FILE} is missing; write it as the write stage describes"], []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return None, [f"{PIECE_FILE} is not valid JSON ({exc})"], []
    if not isinstance(data, dict):
        return None, [f"{PIECE_FILE} must be a JSON object"], []
    piece = PieceFiles(raw=data)
    piece.title = _str(data.get("title"))[:200]
    piece.angle = _str(data.get("angle"))
    piece.angle_reason = _str(data.get("angle_reason"))
    piece.shape = _str(data.get("shape"))
    piece.hook_style = _str(data.get("hook_style"))
    piece.summary = _str(data.get("summary"))
    if piece.shape not in SHAPES:
        problems.append(f"{PIECE_FILE}: shape must be one of {', '.join(SHAPES)}")
    if piece.hook_style and piece.hook_style not in HOOK_STYLES:
        minor.append(f"{PIECE_FILE}: hook_style must be one of {', '.join(HOOK_STYLES)}")
    keys = _angle_keys()
    if keys and piece.angle not in keys:
        # The angle is what the account's results are read by, so it must be a real key.
        minor.append(
            f"{PIECE_FILE}: angle must be the key of one of the library's angles (one of "
            f"{', '.join(sorted(keys))}), not {piece.angle!r}"
        )
    posts = data.get("posts")
    if not isinstance(posts, list) or not posts:
        problems.append(f"{PIECE_FILE}: posts must list the post files in order")
        posts = []
    for i, rel in enumerate(posts, start=1):
        f = _inside(workspace, _str(rel))
        if f is None:
            problems.append(f"{PIECE_FILE}: post {i} path {rel!r} is not inside your folder")
            continue
        if not f.is_file():
            problems.append(f"post {i} file {rel} does not exist")
            continue
        try:
            text = f.read_text(encoding="utf-8-sig").strip()
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"post {i} file {rel} cannot be read as UTF-8 text ({exc})")
            continue
        if not text:
            problems.append(f"post {i} file {rel} is empty")
            continue
        piece.posts.append(text)
        piece.post_files.append(_str(rel))
    cards = data.get("cards") or []
    if not isinstance(cards, list):
        minor.append(f"{PIECE_FILE}: cards must be a list")
        cards = []
    for i, c in enumerate(cards, start=1):
        if not isinstance(c, dict):
            minor.append(f"{PIECE_FILE}: card {i} must be an object")
            continue
        f = _inside(workspace, _str(c.get("file")))
        if f is None or f.suffix.lower() not in (".html", ".htm"):
            minor.append(f"{PIECE_FILE}: card {i} file must be an .html file in your folder")
            continue
        if not f.is_file():
            minor.append(f"card {i} file {c.get('file')} does not exist")
            continue
        # Missing means post 1; 0 or a negative is kept, so the range check names it (a
        # session counting from 0 would otherwise hang every card one post too early).
        try:
            post = 1 if c.get("post") in (None, "") else int(c.get("post"))
        except (TypeError, ValueError, OverflowError):
            post = 1
        piece.cards.append(
            Card(
                html=f,
                png=f.with_suffix(".png"),
                post=post,
                alt=_str(c.get("alt"))[:1000],
                kind=_str(c.get("type")),
            )
        )
    for h in _items(data.get("handles")):
        if isinstance(h, dict) and _str(h.get("handle")):
            handle = _str(h.get("handle")).lstrip("@").lower()
            piece.handles[handle] = _str(h.get("verified_at"))
    piece.companies = [c for c in _items(data.get("companies")) if isinstance(c, dict)]
    piece.recheck = [_str(x) for x in _items(data.get("recheck_before_posting")) if _str(x)]
    return piece, problems, minor


def _angle_keys() -> set[str]:
    """The angle library's keys; empty (no check) when the library cannot be read."""
    try:
        return set(load_angles())
    except Exception:  # a broken angles.yaml is reported where it is loaded, not here
        log.warning("could not read the angle library", exc_info=True)
        return set()


def check_text(piece: PieceFiles, xcfg: dict[str, Any], known_handles: set[str]) -> Report:
    """Everything but the cards: shape, limits, safety, handles, the side files."""
    report = Report(piece=piece)
    headroom = int(xcfg.get("headroom") or 0)
    if not piece.posts:
        report.blocking.append("the piece has no posts")
        return report
    if piece.shape in ("long_post", "short_post") and len(piece.posts) != 1:
        report.blocking.append(
            f"a {piece.shape} is exactly one post; you listed {len(piece.posts)}"
        )
    if piece.shape == "thread" and len(piece.posts) < 2:
        report.blocking.append("a thread needs at least two posts")
    limit = int(
        xcfg.get("thread_post_max") if piece.shape == "thread" else xcfg.get("long_post_max")
    )
    for i, text in enumerate(piece.posts, start=1):
        label = f"post {i}"
        n = x_length(text)
        if n > limit - headroom:
            report.blocking.append(
                f"{label} is {n} characters as X counts them; keep it under {limit - headroom}"
            )
        report.blocking += safety.blocking_problems(text, label)
        if any(line.strip() == "---" for line in text.splitlines()):
            # The approval queue's hand-edit form splits posts on such a line, so an edit
            # there would cut this post in two.
            report.fixable.append(
                f"{label} has a line that is only '---'; the approval queue splits posts "
                "there when the editor edits by hand, so use a blank line or an ALL-CAPS "
                "header between sections"
            )
        for handle in safety.handles_in(text):
            key = handle.lower()
            if key in known_handles:
                continue
            page = piece.handles.get(key, "")
            if not page.startswith(("http://", "https://")):
                report.fixable.append(
                    f"{label} tags @{handle} but {PIECE_FILE} gives no page where you "
                    "verified it; verify it on the organisation's own site or write the name "
                    "without the @"
                )
    if piece.shape == "short_post":
        n = x_length(piece.posts[0])
        soft = int(xcfg.get("short_post_max") or 0)
        if soft and n > soft:
            report.warnings.append(f"the short post runs {n} characters (target under {soft})")
    report.warnings += safety.warnings(piece.posts)
    max_cards = int(xcfg.get("max_cards_total") or 4)
    if len(piece.cards) > max_cards:
        report.blocking.append(f"{len(piece.cards)} cards; at most {max_cards} per piece")
    for c in piece.cards:
        if not 1 <= c.post <= len(piece.posts):
            report.fixable.append(
                f"card {c.html.name} is attached to post {c.post}, which does not exist"
            )
        if not c.alt:
            report.fixable.append(f"card {c.html.name} has no alt text in {PIECE_FILE}")
    per_post: dict[int, int] = {}
    for c in piece.cards:
        per_post[c.post] = per_post.get(c.post, 0) + 1
    for post, n in per_post.items():
        if n > 4:
            report.blocking.append(f"post {post} carries {n} cards; X allows 4 per post")
    if not piece.title:
        report.fixable.append(f"{PIECE_FILE} has no title")
    return report


def check_side_files(workspace: Path, report: Report) -> None:
    for name in (FACTBASE_FILE, FACTCHECK_FILE):
        f = workspace / name
        if not f.is_file() or not f.read_text(encoding="utf-8", errors="replace").strip():
            report.fixable.append(f"{name} is missing or empty")


Renderer = Callable[[Path, Path], render_mod.RenderResult]


def check_cards(piece: PieceFiles, report: Report, renderer: Renderer | None) -> None:
    """Draw every card; layout problems are fixable, a card that cannot be drawn blocks."""
    if not piece.cards:
        return
    if renderer is None:
        report.blocking.append(NO_BROWSER)
        return
    for c in piece.cards:
        try:
            result = renderer(c.html, c.png)
        except render_mod.RenderError as exc:
            report.blocking.append(f"card {c.html.name} could not be drawn: {exc}")
            continue
        size = render_mod.png_size(result.png)
        expected = PNG_SIZES.get(result.size)
        if size is None or (expected and size != expected):
            report.blocking.append(
                f"card {c.html.name} came out {size} instead of {expected}; "
                "size the page to the card"
            )
        report.fixable += [f"card {c.html.name}: {p}" for p in result.problems]
        report.pictures.append(str(c.png))


def check_piece(
    workspace: Path,
    xcfg: dict[str, Any],
    known_handles: set[str],
    renderer: Renderer | None,
    *,
    requested_angle: str = "",
) -> Report:
    """Everything the app checks before a piece may go to the queue. `requested_angle` is
    the angle the editor chose for the piece, if any: the session must write in it."""
    piece, blocking, minor = read_piece(workspace)
    if piece is None:
        report = Report()
        report.blocking += blocking
        return report
    report = check_text(piece, xcfg, known_handles)
    report.blocking += blocking
    report.fixable += minor
    if requested_angle and piece.angle != requested_angle:
        report.fixable.append(
            f"{PIECE_FILE}: the editor chose the angle {requested_angle!r} for this piece; "
            f"write it in that angle (piece.json says {piece.angle!r})"
        )
    check_side_files(workspace, report)
    check_cards(piece, report, renderer)
    return report
