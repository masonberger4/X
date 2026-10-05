"""The studio's prompts (pure: no DB, no network, no clock; every input is a parameter).

The session's standing instructions travel once, as text appended to Claude Code's own
system prompt (`system_prompt`): who it is, how a piece is made, the voice guide and the
card spec. The CLI records that system prompt with the session, so every resume keeps
it. Each stage then gets its own user prompt with the specifics: the topic, the angles on
offer, what recent pieces did, the playbook, the files to write and when to stop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from studio.angles import HOOK_STYLES, SHAPES, AngleOffer

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
    """A piece the account already made, so the next one does not repeat it."""

    date: str
    title: str
    angle: str = ""
    shape: str = ""
    hook_style: str = ""
    opening: str = ""
    companies: list[str] = field(default_factory=list)

    def line(self) -> str:
        bits = [self.date, self.title]
        tags = ", ".join(x for x in (self.angle, self.shape, self.hook_style) if x)
        if tags:
            bits.append(f"({tags})")
        out = " · ".join(bits)
        if self.companies:
            out += f" · companies: {', '.join(self.companies)}"
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
    recent: list[RecentPiece] = field(default_factory=list)
    playbook: str = ""
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
    if b.shortlist:
        lines.append(
            "These are the top stories from the account's feeds that no piece has used "
            "yet. Pick one of them, or run your own news scan (web search) and pick "
            "something better; say which and why in research.json."
        )
        lines += [s.block() for s in b.shortlist]
    else:
        lines.append("Run a news scan with web search to find it.")
    return "\n".join(lines)


def _recent_block(b: Brief) -> str:
    if not b.recent:
        return "(none yet)"
    return "\n".join(p.line() for p in b.recent)


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
    "why_now": "one sentence",
    "companies": [{{"name": "...", "ticker": "MRK or 9926.HK or null"}}],
    "candidate_angles": [{{"angle": "<angle key from the list below>", "why": "..."}}],
    "summary": "two or three sentences for the editor"}}
   Give two or three candidate angles, best first.

ANGLES ON OFFER
{_angles_block(b)}

RECENT PIECES (do not repeat a topic unless there is genuinely new news on it)
{_recent_block(b)}

Do not write the post in this stage. When both files are written, end your turn with a
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
    return "\n".join(lines) if lines else "(no recent pieces)"


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
   that shows it. Fix every real problem. Write {FACTCHECK_FILE}: a table of every
   finding (post or card, what it said, the problem, the source, what you changed or why
   you kept it), then the checks that passed, then anything still unverified.
4. {PIECE_FILE}, exactly this shape:
{_piece_schema(b)}
   "shape" is one of {", ".join(SHAPES)}; "hook_style" one of the listed styles.
   "handles" lists every @handle in the posts with the page that verified it.

ANGLES ON OFFER
{_angles_block(b)}

VARIETY
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


def revise_prompt(note: str) -> str:
    return f"""REVISION

The editor read the finished piece and asks for changes:
{note.strip()}

Make them. Update the posts, the cards and {PIECE_FILE}; update {FACTBASE_FILE} if the
facts change. Run the cold fact-check again (a fresh sub-agent, as before) on everything
new or changed, and add its findings to {FACTCHECK_FILE}. End your turn with a short
summary of what you changed. The app checks the piece again afterwards."""


def resume_prompt(stage: str, reason: str, original: str) -> str:
    """Re-send a stage's instructions after its run was interrupted."""
    return f"""Your previous run of this stage was interrupted ({reason}). Some files in your
working folder may be missing or half-written. Check what is there, then finish the stage.
The stage's instructions, again:

{original}"""
