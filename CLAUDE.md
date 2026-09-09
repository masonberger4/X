# CLAUDE.md

Project guidance for Claude Code. Read PLAN.md before making changes.

## What this is
A human-in-the-loop pipeline that ingests oncology news, scores it with the
Anthropic API, and drafts X posts for human approval. Python 3.11+, SQLite.
Step 1 (ingest + dedup + prefilter + score + digest), step 2 (draft + human
approval queue) and step 3 (publish to X) are implemented. Nothing posts
unless `PUBLISH_ENABLED=1` **and** `--live`. Step 4 (feedback loop) is not built.

## Commands
- Install: `pip install -e ".[dev]"`
- Lint: `ruff check .` and `ruff format --check .`
- Test: `pytest`
- Run: `python run_ingest.py`, `python run_score.py`, `python digest.py [--rate]`,
  `python run_draft.py`, `python run_queue.py` (approval UI on localhost:8000),
  `python run_publish.py` (dry run by default; `--live` needs `PUBLISH_ENABLED=1`)

## Rules
- **Config drives everything.** Feeds, queries, company list, keywords,
  cadences, thresholds, and model names live in `config.yaml`. Never hardcode
  queries, URLs, feeds, or `claude-*` model IDs in code. `config.load_config()`
  expands `companies.feeds` into `rss` sources named `company_<key>`.
- **Network I/O is confined** to `ingest/http.py` (`get_text`, `get_json`),
  `PubMedSource.esearch/efetch` (Entrez), and `Scorer.create_message`
  (Anthropic), `draft/drafter.py:call_anthropic`, and `publish/client.py`
  (`post_tweet`, `verify_credentials`; the only place tweepy is imported, inside
  the functions). Tests monkeypatch those and never hit the network.
- **Tests use real saved feeds** in `tests/fixtures/` where a live sample could
  be captured; synthetic fixtures only for bot-protected endpoints (FDA OCE
  page, ClinicalTrials.gov API).
- **Dedup order:** exact `dedup_hash` (sha256 of normalized title+url) -> DOI
  match -> near-duplicate normalized title (`difflib`, cross-source only, with
  a differing-word guard). One cluster = one story; clusters keep the earliest
  `published_at`.
- **Every score row stores** `model`, `prompt_version`
  (`score/rubric.py:PROMPT_VERSION`), and the raw API response. Bump
  `PROMPT_VERSION` whenever the prompt, few-shot examples, or tool schema
  change; `run_score.py` then re-scores automatically.
- **Scoring is tool-use with a strict JSON schema** (`score/rubric.py:TOOL`).
  `total` is computed in code (`compute_total`), never taken from the model.
- **NCBI etiquette:** `Entrez.email`/`tool` from `config.ncbi` (`NCBI_EMAIL`
  env overrides); rate limiter at 3 req/s, or 10 req/s with `NCBI_API_KEY`.
- **Cadence:** `run_ingest.py` only fetches sources whose last run (table
  `source_runs`) is older than `cadence_minutes`; `--force` overrides.
- **Fail soft per source.** Errors are logged and recorded in
  `source_runs.error`; the run continues. `ingest/fda_oce.py` returns `[]` on
  any failure.
- Read secrets from `.env` via python-dotenv; never commit `.env`.
- Logging: stdlib `logging`. INFO for per-source counts, DEBUG for items.
- Ask before adding a dependency not already in `pyproject.toml`.
- Generated content must never contain medical advice. Preprints are labelled
  as preprints. `draft/drafter.py:check_hard_rules` enforces this in code after
  generation (plus 280 chars/post with URLs as 23, source URL placement, and
  verbatim-number verification); drafts that fail are stored as `failed`.
- Step 2 reads step 1's tables only through
  `approval_queue/store.py:fetch_candidates` (one candidate per cluster). Its own
  tables are `drafts` and `decisions`; edits log original vs edited text.
- Step 3 reads step 2's tables only through `publish/store.py:fetch_approved`
  (edited_text from `decisions` wins over `single_post`). Its own tables are
  `schedule` (claim row, one per draft) and `posts` (one row per tweet). Its
  settings live in `publish/config.yaml`, not the root config. Posting is
  idempotent via the claim; partial threads are never retried automatically.
  Texts are re-checked before posting and refused, never edited, on failure.
- Commit after each working module.

## Layout
```
ingest/   base.py (Item, Source ABC), http.py, rss.py, biorxiv.py, pubmed.py,
          clinicaltrials.py, fda_oce.py
filter/   prefilter.py, dedup.py
score/    rubric.py, scorer.py
db.py     sqlite: items, clusters, scores, ratings, source_runs
draft/    schema.py, prompt.py, voice.md, drafter.py
approval_queue/  store.py (drafts, decisions, fetch_candidates), app.py, templates/
publish/  config.yaml, scheduler.py, thread.py, store.py (schedule, posts,
          fetch_approved), client.py
run_ingest.py  run_score.py  digest.py  run_draft.py  run_queue.py
run_publish.py   (CLIs)
```
