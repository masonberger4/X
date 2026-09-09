# CLAUDE.md

Project guidance for Claude Code. Read PLAN.md before making changes.

## What this is
A human-in-the-loop pipeline that ingests oncology news, scores it with the
Anthropic API, and drafts X posts for human approval. Python 3.11+, SQLite.
Step 1 (ingest + dedup + prefilter + score + digest) is implemented. Drafting,
posting, and any X API integration are deliberately **not** built yet.

## Commands
- Install: `pip install -e ".[dev]"`
- Lint: `ruff check .` and `ruff format --check .`
- Test: `pytest`
- Run: `python run_ingest.py`, `python run_score.py`, `python digest.py [--rate]`

## Rules
- **Config drives everything.** Feeds, queries, company list, keywords,
  cadences, thresholds, and model names live in `config.yaml`. Never hardcode
  queries, URLs, feeds, or `claude-*` model IDs in code. `config.load_config()`
  expands `companies.feeds` into `rss` sources named `company_<key>`.
- **Network I/O is confined** to `ingest/http.py` (`get_text`, `get_json`),
  `PubMedSource.esearch/efetch` (Entrez), and `Scorer.create_message`
  (Anthropic). Tests monkeypatch those and never hit the network.
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
  as preprints.
- Commit after each working module.

## Layout
```
ingest/   base.py (Item, Source ABC), http.py, rss.py, biorxiv.py, pubmed.py,
          clinicaltrials.py, fda_oce.py
filter/   prefilter.py, dedup.py
score/    rubric.py, scorer.py
db.py     sqlite: items, clusters, scores, ratings, source_runs
run_ingest.py  run_score.py  digest.py   (CLIs)
```
