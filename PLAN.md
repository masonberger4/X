# Cancer Research X Account — AI-Assisted Pipeline

Goal: an X account on the business and investing side of immuno-oncology
biotech: CAR-T and other engineered cell therapies, T-cell engagers and
bispecifics, and adjacent cutting-edge immuno-oncology science. It covers
clinical trial results and what they mean, upcoming readouts and catalysts
(especially for publicly traded companies), and M&A, licensing and financing in
the space. Equal weight on the science and the investment implications. The AI
writes as a PhD-level immuno-oncology analyst at a hedge fund would: precise
about mechanism and trial design, explicit about what a result does to a
company's thesis, never a stock tip. An AI pipeline monitors sources, scores
them, and drafts posts; a human approves, edits, and adds commentary before
publishing. Target X's Original Content Rewards program (500 verified
followers, 500k Home Timeline impressions from Premium users per 90 days,
content must add interpretation — not description).

Audience: biotech investors and analysts (buy side, sell side, generalists
learning the space), company operators and BD teams, and clinician-scientists
who follow the money.

## Principles
- Human-in-the-loop by default. Autonomy is a dial, not a starting point.
- Every post must contain an interpretation (what it means, what to watch, what's overhyped).
- No medical advice or treatment recommendations. Ever.
- No investment advice: no buy/sell/hold/short calls, no price targets, no
  return promises. Describe what a result means for a thesis and the risks;
  the reader decides. Bio discloses that nothing is investment advice.
- Always link the primary source. Label preprints as preprints.
- Never fabricate numbers; stats are pulled verbatim from the source.
- Bio discloses AI-assisted drafting.

## Pipeline

### 1. Ingest (cron, every 30–60 min)
Sources:
- PubMed E-utilities — saved queries: oncology, immunotherapy, CAR-T, ADCs, bispecifics, cell therapy, gene editing
- bioRxiv / medRxiv RSS (cancer biology, oncology)
- ClinicalTrials.gov change feed (oncology)
- FDA press releases; Oncology Center of Excellence approvals
- Company PR/IR feeds for ~40 oncology companies
- ASCO / AACR / ESMO / ASH abstract releases
- Curated X list of ~50 oncology KOLs
Store in SQLite: id, source, url, title, abstract, published_at, fetched_at, dedup_hash. Skip seen items.

### 2. Score (cheap model, e.g. claude-haiku)
Rubric 0–10 on: novelty, clinical significance, audience interest, expertise fit
(cell therapy / gene editing bonus), timeliness. Threshold gate; top ~5–10/day
advance. Log every score for later tuning.

### 3. Draft (stronger model, claude-sonnet / claude-opus)
Inputs: source text, voice guide (tone, sample posts, banned phrases), and the
originality rule (interpretation required).
Output JSON: thread (3–6 posts), exactly one of chart / table, suggested_visual,
why_it_matters, claims_to_verify (confidence flags).
Hard rules in prompt: no medical advice, link primary source, label preprints,
no fabricated numbers.

### 4. Approval queue (human step)
Minimal web UI or Telegram/Slack bot showing draft, source, score.
Actions: approve / edit / reject / snooze. ~10 min per day.
Edit history is saved as voice-guide training data.

### 5. Publish (X API v2 via tweepy)
2–3 daily slots in the configured time zone (`timezone:` in
`publish/config.yaml`, shipped as US Pacific), aimed at the US working day,
plus immediate post for breaking items
(e.g. FDA approvals). Bio discloses AI assistance.

### 6. Feedback loop (weekly)
Pull impressions, engagement, follower delta per post. Feed back into the
scoring rubric: which sources, formats, topics perform with Premium users.

## Stack
Python 3.12, SQLite, feedparser, biopython (Entrez), httpx, FastAPI (queue UI),
tweepy, cron or a small VPS. Anthropic API for scoring/drafting.
Budget: ~$30–60/mo API + X Premium.

## Build order
1. Ingest + score. Read the daily digest for a week before writing any posts.
2. Draft + approval queue.
3. Publish.
4. Feedback loop.

---

# Step 1 Kickoff Prompt (paste into Claude Code)

```
You are building step 1 of the pipeline described in PLAN.md: the ingest and
scoring layer for a cancer-research news pipeline. Read PLAN.md first.

Build a Python 3.12 project with this structure:

  ingest/
    __init__.py
    base.py          # Source ABC: fetch() -> list[Item]
    pubmed.py        # Entrez esearch/efetch, saved queries from config
    biorxiv.py       # RSS via feedparser (biorxiv + medrxiv)
    clinicaltrials.py# ClinicalTrials.gov API v2, oncology, recently updated
    fda.py           # FDA press release RSS + OCE approvals page
    company_pr.py    # generic RSS ingester driven by config list
  score/
    __init__.py
    rubric.py        # prompt + JSON schema for scoring
    scorer.py        # calls Anthropic API (claude-haiku), batches items
  db.py              # SQLite via sqlite3; items + scores tables; dedup on hash
  config.yaml        # queries, RSS urls, company feeds, score threshold
  run_ingest.py      # CLI: fetch all sources, insert new items
  run_score.py       # CLI: score unscored items, write to scores table
  digest.py          # CLI: print/markdown top-N items for the last 24h
  tests/             # pytest; mock network calls, test dedup and parsing
  pyproject.toml, README.md, .env.example

Requirements:
- Item dataclass: id, source, url, title, abstract, published_at, fetched_at,
  dedup_hash (sha256 of normalized title+url), raw_json.
- Never insert duplicates; dedup_hash is UNIQUE.
- Scoring rubric returns JSON: novelty, clinical_significance,
  audience_interest, expertise_fit, timeliness (each 0–10), total, one-line
  rationale, and suggested_angle. Bonus for cell therapy / gene editing.
- Use the Anthropic SDK; read ANTHROPIC_API_KEY from .env. Batch items into
  one call where sensible. Handle rate limits with retry/backoff.
- Respect NCBI etiquette: Entrez.email set from config, <=3 req/s.
- All network I/O behind small functions so tests can mock them.
- Config drives everything; no hardcoded queries or URLs in code.
- Log with the stdlib logging module. Keep dependencies minimal.

Start by proposing the config.yaml schema and db schema, then implement
pubmed.py and db.py first with tests, run the tests, then continue source by
source. Ask me before adding any dependency not listed above.

Do not build drafting, posting, or any X API integration yet.
```

### 9. Swarm (takes the human out of the creative loop)
Premise ("more is different"): a single cheap model is a poor analyst, but
many cheap calls with narrow jobs, local rules and no view of the whole,
arranged in layers, may beat one strong call. So no agent writes a thread:
a genome names the slots and each slot's job; cheap cells propose, cheap
synthesisers improve on every earlier candidate, cheap judges run a
tournament per slot, one assembly call produces the step 3 JSON through the
same hard rules. The human keeps one job: when a post goes out.
- Phase one (built, `swarm/`): cells + layers + jury against the single
  strong drafter as a CONTROL on every story; both variants and the verdict
  are recorded (`swarm_runs`, `swarm_variants`).
- Phase two (built, `run_evolve.py`): fitness from X. Join step 4's metrics
  to the run; relative KPI (post / trailing 30-day median); the jury's
  swarm-vs-control verdicts measured against reality; three seed genomes
  drafted round-robin; below-median genomes retired.
- Phase three (built, `run_evolve.py breed`, `swarm/mutate.py`): mutation.
  One strong call writes a child genome that varies ONE thing (a slot rule,
  a split or merge, fan-out, layers), checked in code; designer genomes own
  a chart Style, are drawn round-robin, scored and pruned the same way, and
  bred by stepping one knob. The panel's /swarm page shows the family tree.
  Selection acts on the topology, so a single strong call can win back if
  that is what X rewards.
- Phase four (built): the format is a gene. Thread, single post or Premium
  long post; zero, one or two pictures and which post each goes on, as a
  third population with its own, higher pruning bar. The drafter, the
  checks, the queue and publishing read the draft's own format; a draft
  from before phase four keeps the old physics.
Full specification: `prompts/prompt9.md`.
