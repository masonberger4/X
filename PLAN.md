# Cancer Research X Account — AI-Assisted Pipeline

Goal: a cancer-research-focused X account where an AI pipeline monitors sources,
scores them, and drafts posts; a human approves, edits, and adds commentary
before publishing. Target X's Original Content Rewards program (500 verified
followers, 500k Home Timeline impressions from Premium users per 90 days,
content must add interpretation — not description).

## Principles
- Human-in-the-loop by default. Autonomy is a dial, not a starting point.
- Every post must contain an interpretation (what it means, what to watch, what's overhyped).
- No medical advice or treatment recommendations. Ever.
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
Output JSON: single_post, thread (3–6 posts), suggested_visual,
why_it_matters, claims_to_verify (confidence flags).
Hard rules in prompt: no medical advice, link primary source, label preprints,
no fabricated numbers.

### 4. Approval queue (human step)
Minimal web UI or Telegram/Slack bot showing draft, source, score.
Actions: approve / edit / reject / snooze. ~10 min per day.
Edit history is saved as voice-guide training data.

### 5. Publish (X API v2 via tweepy)
2–3 daily slots (US morning/lunch) plus immediate post for breaking items
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
