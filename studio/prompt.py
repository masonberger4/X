"""The studio's prompts (pure: no DB, no network, no clock; every input is a parameter).

The session's standing instructions travel once, as text appended to Claude Code's own
system prompt (`system_prompt`): who it is, how a piece is made, the voice guide and the
card spec. The CLI records that system prompt with the session, so every resume keeps
it. Each stage then gets its own user prompt with the specifics: the topic, the angles on
offer, what recent pieces did, the playbook, the files to write and when to stop.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from studio.angles import HOOK_STYLES, SHAPES, AngleOffer
from studio.learn import Lean
from studio.radar import KINDS, Catalyst, Topic

PIECE_FILE = "piece.json"
RESEARCH_FILE = "research.json"
FACTBASE_FILE = "factbase.md"
FACTCHECK_FILE = "factcheck.md"
POSTS_DIR = "posts"
CARDS_DIR = "cards"


@dataclass
class Story:
    """A story from the feeds (step 1), as the session is told about it."""

    cluster_id: int
    title: str
    url: str = ""
    source: str = ""
    published: str = ""
    summary: str = ""
    score: int | None = None
    why: str = ""

    def block(self) -> str:
        lines = [f"- [story {self.cluster_id}] {self.title}"]
        meta = ", ".join(x for x in (self.source, self.published) if x)
        if meta:
            lines.append(f"  {meta}")
        if self.url:
            lines.append(f"  {self.url}")
        if self.score is not None:
            lines.append(f"  feed score {self.score}/50" + (f": {self.why}" if self.why else ""))
        if self.summary:
            lines.append(f"  {' '.join(self.summary.split())[:900]}")
        return "\n".join(lines)


@dataclass
class RecentPiece:
    """A piece the account already made, or is still making, so the next one does not
    repeat it."""

    date: str
    title: str
    angle: str = ""
    shape: str = ""
    hook_style: str = ""
    opening: str = ""
    companies: list[str] = field(default_factory=list)
    status: str = ""  # '' once it reached the queue; else how far it got

    def line(self) -> str:
        bits = [self.date, self.title]
        tags = ", ".join(x for x in (self.angle, self.shape, self.hook_style) if x)
        if tags:
            bits.append(f"({tags})")
        out = " · ".join(bits)
        if self.companies:
            out += f" · companies: {', '.join(self.companies)}"
        if self.status:
            out += f" · {self.status}"
        if self.opening:
            out += f'\n    opened with: "{" ".join(self.opening.split())[:200]}"'
        return f"- {out}"


@dataclass
class Brief:
    """Everything a stage prompt needs about the piece and the account."""

    piece_id: int
    today: str  # the local date, e.g. "2026-10-05"
    timezone: str
    workspace: str  # the session's working folder (absolute)
    reference_dir: str  # the reference pieces (absolute)
    references: list[str]  # folder names of the reference pieces
    topic: str = ""  # what a human asked for, '' when the session chooses
    story: Story | None = None  # the feed story the piece starts from
    shortlist: list[Story] = field(default_factory=list)  # stories to choose from
    offer: AngleOffer | None = None
    hooks_to_avoid: list[str] = field(default_factory=list)
    # The last written pieces, which the write stage varies its shape and opening from.
    recent: list[RecentPiece] = field(default_factory=list)
    # Every piece of the last few days, finished or still being made, whose topic the
    # research stage must not repeat.
    topics_to_avoid: list[RecentPiece] = field(default_factory=list)
    playbook: str = ""
    # The radar (studio/radar.py), for a piece that chooses its own topic: today's scan
    # topics as (radar id, topic), and the catalysts coming up or just passed.
    radar: list[tuple[int, Topic]] = field(default_factory=list)
    coming_up: list[Catalyst] = field(default_factory=list)
    # What X says about the account's earlier pieces (studio/evidence.py): the evidence
    # text and this piece's lean, both empty until enough pieces are measured.
    evidence: str = ""
    lean: Lean | None = None
    long_post_max: int = 25000
    thread_post_max: int = 25000
    short_post_max: int = 1000
    max_cards: int = 4


def system_prompt(session: str, voice: str, cards: str) -> str:
    """The standing instructions appended to Claude Code's system prompt."""
    return "\n\n".join(part.strip() for part in (session, voice, cards) if part and part.strip())


# --- research ---------------------------------------------------------------------------


def _topic_block(b: Brief) -> str:
    if b.story is not None:
        head = "Start from this story from the account's feeds."
        if b.topic:
            head += f" The editor adds: {b.topic}"
        return f"{head}\n{b.story.block()}"
    if b.topic:
        return (
            f"The editor asked for a piece on: {b.topic}\n"
            "Work out what the most interesting, newsworthy question in that topic is "
            "right now, and research that."
        )
    lines = [
        "Choose the topic yourself. Find the most market-moving or most interesting "
        "immuno-oncology story of the last few days for this audience: a readout, a deal, "
        "a regulatory decision, a financing, a catalyst coming up, or a pattern across "
        "several of them."
    ]
    if b.radar:
        lines.append(
            "THE RADAR: topics today's scan of the news proposed (no piece has used them). "
            "Check its facts yourself; its why-now is a lead, not a source."
        )
        lines += [_radar_line(rid, t) for rid, t in b.radar]
    if b.coming_up:
        lines.append(
            "COMING UP: dated catalysts on the account's calendar. A preview before an "
            "event or a reaction just after one makes a strong piece."
        )
        lines += [f"- {c.line()}" for c in b.coming_up]
    if b.shortlist:
        lines.append(
            "THE FEEDS: the top scored stories that the account has not written about yet "
            "(no piece and no drafted thread)."
        )
        lines += [s.block() for s in b.shortlist]
    if b.radar or b.coming_up or b.shortlist:
        lines.append(
            "Pick one of these, or run your own news scan (web search) and pick something "
            "better; say which and why in research.json."
        )
    else:
        lines.append("Run a news scan with web search to find it.")
    return "\n".join(lines)


def _radar_line(radar_id: int, t: Topic) -> str:
    lines = [f"- [radar {radar_id}] {t.title}"]
    if t.why_now:
        lines.append(f"  Why now: {t.why_now}")
    tags = []
    if t.angle:
        tags.append(f"suggested angle {t.angle}")
    if t.companies:
        tags.append("companies " + ", ".join(c.label() for c in t.companies))
    if tags:
        lines.append("  " + "; ".join(tags))
    if t.sources:
        lines.append("  " + " ".join(t.sources))
    return "\n".join(lines)


def _recent_block(b: Brief) -> str:
    if not b.topics_to_avoid:
        return "(none yet)"
    return "\n".join(p.line() for p in b.topics_to_avoid)


def _angles_block(b: Brief) -> str:
    if b.offer is None or not b.offer.angles:
        return "(any)"
    lines = [a.brief() for a in b.offer.angles]
    if b.offer.forced:
        lines.insert(0, "The editor chose this angle for the piece:")
    elif b.offer.held_back:
        lines.append(
            "Not on offer because recent pieces used them: " + ", ".join(b.offer.held_back)
        )
    return "\n".join(lines)


def _x_says(b: Brief) -> str:
    """How the account's pieces did on X, as a section of its own; "" before any was
    measured, so a new account's prompts carry no empty heading."""
    body = [x for x in (b.evidence.strip(), b.lean.line() if b.lean else "") if x]
    if not body:
        return ""
    return (
        "WHAT X SAYS (how this account's earlier pieces did with readers; weigh it with "
        "the story, which comes first)\n" + "\n".join(body) + "\n\n"
    )


def _paths_block(b: Brief) -> str:
    refs = ", ".join(b.references) if b.references else "(none)"
    return (
        f"Today is {b.today} ({b.timezone}). This is piece {b.piece_id}.\n"
        f"Your working folder: {b.workspace}\n"
        f"The reference folder: {b.reference_dir} (reference pieces: {refs}; each has a "
        "handoff.md and a cards/ folder)"
    )


def research_prompt(b: Brief) -> str:
    return f"""STAGE 1 OF 3: RESEARCH

{_paths_block(b)}

THE TOPIC
{_topic_block(b)}

WHAT TO DO
Read the reference pieces' handoff docs first if you have not yet in this session. Then
research the topic properly: primary sources first, then trade press and analysts, then
market forecasts (sceptically). Keep going until you could defend every number in the
piece to a sceptical buy-side analyst.

WHAT TO WRITE IN YOUR WORKING FOLDER
1. {FACTBASE_FILE}: the fact base, in the spirit of the reference handoff docs:
   - a short summary of the story and why it matters now, "as of {b.today}";
   - sections with tables for what the piece will need (the event, the data, the
     competitors and their stage, the market, deals and financings, catalysts with
     dates); include only sections the story needs;
   - every fact with its source URL and "(opened)" or "(snippet)"; facts from your own
     knowledge marked "(knowledge)";
   - "X handles": each @handle you verified on the organisation's own website, with the
     page; organisations whose site links no X account, so they get a ticker or plain
     name instead;
   - "Corrections": anything you found wrong along the way;
   - "Open questions": what you could not confirm.
2. {RESEARCH_FILE}: JSON with
   {{"topic": "the piece's subject in one line",
    "story_id": <the story number if you used one of the listed stories, else null>,
    "radar_topic": <the radar number if you used one of the radar's topics, else null>,
    "why_now": "one sentence",
    "companies": [{{"name": "...", "ticker": "MRK or 9926.HK or null"}}],
    "candidate_angles": [{{"angle": "<angle key from the list below>", "why": "..."}}],
    "catalysts": [{{"date": "2026-11-14 or 2026-11 or Q4 2026", "company": "...",
                   "ticker": "...", "drug": "...", "kind": "{" | ".join(KINDS)}",
                   "detail": "one sentence", "source": "https://..."}}],
    "summary": "two or three sentences for the editor"}}
   Give two or three candidate angles, best first. Under "catalysts" list every dated
   upcoming event you confirmed (PDUFA dates, readouts, conference presentations,
   advisory committees) with its source: the account keeps a catalyst calendar from them.

ANGLES ON OFFER
{_angles_block(b)}

RECENT PIECES (do not repeat a topic unless there is genuinely new news on it)
{_recent_block(b)}

{_x_says(b)}Do not write the post in this stage. When both files are written, end your turn with a
three-line summary: the topic, the angle you lean towards, and anything the editor should
know before you write."""


# --- write ------------------------------------------------------------------------------


def _piece_schema(b: Brief) -> str:
    example = {
        "title": "internal title, under 100 characters",
        "angle": "<angle key>",
        "angle_reason": "one sentence",
        "shape": "long_post | thread | short_post",
        "hook_style": " | ".join(HOOK_STYLES),
        "posts": [f"{POSTS_DIR}/01.txt"],
        "cards": [
            {
                "file": f"{CARDS_DIR}/card_1.html",
                "post": 1,
                "type": "headline_stat",
                "alt": "what the card shows, for screen readers, under 1000 characters",
            }
        ],
        "companies": [{"name": "Merck", "ticker": "MRK"}],
        "handles": [{"handle": "@Merck", "verified_at": "https://www.merck.com/"}],
        "recheck_before_posting": ["fast-moving facts to re-check on posting day"],
        "summary": "two sentences for the editor",
    }
    return json.dumps(example, indent=2, ensure_ascii=False)


def _variety_block(b: Brief) -> str:
    lines = []
    if b.hooks_to_avoid:
        lines.append(
            "Hook styles used by the last pieces (use a different one): "
            + ", ".join(b.hooks_to_avoid)
        )
    shapes = [p.shape for p in b.recent[:3] if p.shape]
    if len(shapes) >= 2 and len(set(shapes)) == 1:
        lines.append(
            f"The last {len(shapes)} pieces were all {shapes[0]}; prefer another shape if "
            "the story allows it."
        )
    openings = [p.opening for p in b.recent if p.opening][:5]
    if openings:
        lines.append("Openings of recent pieces (do not echo their wording or rhythm):")
        lines += [f'- "{" ".join(o.split())[:200]}"' for o in openings]
    if lines:
        return "\n".join(lines)
    return "(nothing recent to vary from)" if b.recent else "(no recent pieces)"


def write_prompt(b: Brief, note: str = "") -> str:
    editor = f"THE EDITOR READ YOUR FACT BASE AND SAYS\n{note.strip()}\n\n" if note.strip() else ""
    return f"""STAGE 2 OF 3: WRITE

{_paths_block(b)}

{editor}CHOOSE
- The angle: one from the list below, the one this story makes most interesting.
- The shape: long_post (one post up to {b.long_post_max} characters), thread (two or more
  long posts, each up to {b.thread_post_max}; each post must stand on its own as a
  section), or short_post (one post, under about {b.short_post_max} characters, carried by
  one card). Length should fit the story: never pad.
- The hook: the first 280 characters, written to make a specialist stop scrolling.

WRITE
1. The post as plain text files in {POSTS_DIR}/: {POSTS_DIR}/01.txt for a single post,
   then {POSTS_DIR}/02.txt and on for a thread, one file per post in order. Exactly
   what will be posted: no markdown, no file headers, no links.
2. The cards as HTML in {CARDS_DIR}/ ({CARDS_DIR}/card_1.html and on, at most
   {b.max_cards}), following the card spec. Each card names the post it attaches to
   (normally post 1). Fewer, better cards beat more cards.
3. The cold fact-check. Start a fresh sub-agent with the Agent tool. Give it the full
   text of every post and every card's visible text (not your fact base or your notes)
   and tell it to check every claim, number, date, name, title, stage and handle against
   primary sources on the web, independently, and to report each problem with the source
   that shows it. Run it in the foreground and wait for its report: the stage is not
   done while it is still checking. Fix every real problem. Write {FACTCHECK_FILE}: a
   table of every finding (post or card, what it said, the problem, the source, what you
   changed or why you kept it), then the checks that passed, then anything still
   unverified.
4. {PIECE_FILE}, exactly this shape:
{_piece_schema(b)}
   "shape" is one of {", ".join(SHAPES)}; "hook_style" one of the listed styles.
   "handles" lists every @handle in the posts with the page that verified it.

ANGLES ON OFFER
{_angles_block(b)}

{_x_says(b)}VARIETY
{_variety_block(b)}

THE PLAYBOOK (what has worked on this account; it wins over the voice guide)
{b.playbook.strip() or "(empty)"}

When the files are written, end your turn with a short summary for the editor. The app
then draws your cards, counts characters the way X does and runs its checks, and sends
you what it finds."""


# --- polish -----------------------------------------------------------------------------


def polish_prompt(
    *,
    round_no: int,
    max_rounds: int,
    problems: list[str],
    pictures: list[str],
    review_only: bool = False,
) -> str:
    """The app's report on the piece, for the session to fix."""
    shown = "\n".join(f"- {p}" for p in pictures) or "- (no cards)"
    if review_only:
        head = (
            "The app drew your cards and found no problems in its checks. Open every "
            "picture with Read and look at it the way a reader on a phone will: is the "
            "headline the finding, is everything legible, is anything crowded, clipped, "
            "misaligned or visually weak? Fix any card that is not as good as the reference "
            "cards. If they are all good, change nothing."
        )
        problem_block = ""
    else:
        head = (
            "The app drew your cards and checked the piece. Fix every problem below, "
            "then open every picture with Read and look at it."
        )
        problem_block = "\nPROBLEMS\n" + "\n".join(f"- {p}" for p in problems) + "\n"
    return f"""STAGE 3 OF 3: POLISH (round {round_no} of {max_rounds})

{head}
{problem_block}
CARD PICTURES
{shown}

Change only what is needed. Keep {PIECE_FILE} in step with the files. End your turn
with one line: what you changed, or "no changes"."""


# --- revise and resume ------------------------------------------------------------------


def _hand_edits_block(edited_posts: Sequence[str], dropped_cards: Sequence[str]) -> str:
    """What the editor changed in the approval queue after the session finished, which the
    app wrote into the session's files before this revision."""
    lines = []
    if edited_posts:
        lines.append(
            "The editor changed the text by hand in the approval queue after you finished "
            f"it. Before this revision the app wrote the editor's version into your post "
            f"files ({', '.join(edited_posts)}) and listed them in {PIECE_FILE}: that "
            "version is what would be posted. Keep every change the editor made, word for "
            "word, unless the request above asks to change that part; make the requested "
            "changes on top of it and never put your earlier wording back. If the "
            "fact-check finds a problem in a line the editor wrote, keep the line and say "
            f"what the problem is in {FACTCHECK_FILE} and in your summary."
        )
    if dropped_cards:
        lines.append(
            "The editor dropped these cards from the post in the approval queue: "
            + "; ".join(dropped_cards)
            + f". The app took them out of {PIECE_FILE}'s card list (their files are still "
            "in your folder). Leave them out unless the request above asks for them back."
        )
    if not lines:
        return ""
    return "\nTHE EDITOR'S OWN CHANGES IN THE QUEUE\n" + "\n".join(lines) + "\n"


def revise_prompt(
    note: str, *, edited_posts: Sequence[str] = (), dropped_cards: Sequence[str] = ()
) -> str:
    """The editor's request, and the changes the editor made by hand in the queue (the app
    has already put those into the session's files; see studio/session.py:write_back)."""
    asked = note.strip() or "(nothing beyond keeping the changes below)"
    return f"""REVISION

The editor read the finished piece and asks for changes:
{asked}
{_hand_edits_block(edited_posts, dropped_cards)}
Make them. Update the posts, the cards and {PIECE_FILE}; update {FACTBASE_FILE} if the
facts change. Run the cold fact-check again (a fresh sub-agent in the foreground, as
before) on everything new or changed, and add its findings to {FACTCHECK_FILE}. End
your turn with a short summary of what you changed. The app checks the piece again
afterwards."""


def resume_prompt(stage: str, reason: str, original: str) -> str:
    """Re-send a stage's instructions after its run was interrupted."""
    return f"""Your previous run of this stage was interrupted ({reason}). Some files in your
working folder may be missing or half-written. Check what is there, then finish the stage.
The stage's instructions, again:

{original}"""


def fresh_session_prompt(stage: str, original: str) -> str:
    """A stage's instructions for a new session taking a piece over from one the CLI no
    longer has (its stored conversation was cleaned up): what the old session knew is gone,
    what it made is in the working folder, so the new one reads that first."""
    return f"""PICKING UP A PIECE

This piece was started in an earlier session that can no longer be resumed, so you are
taking it over in a new one. Everything that session made is in your working folder:
{FACTBASE_FILE} and {RESEARCH_FILE} (its research), and once the piece was written
{POSTS_DIR}/, {CARDS_DIR}/, {PIECE_FILE} and {FACTCHECK_FILE} (the cold fact-check log).
Read every one of them that exists before you do anything else: they are the piece's own
work so far, and the fact base is what it stands on. Read the reference pieces' handoff
docs too if this stage needs them. Then do this stage ({stage}).
The stage's instructions, again:

{original}"""
