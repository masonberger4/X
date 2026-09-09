# CLAUDE.md — conventions for this repo

Step 1 (ingest + score + digest) of the pipeline in PLAN.md. Read PLAN.md before
changing scope. Drafting, posting, and any X API integration are deliberately
**not** built yet.

## Conventions (keep these)

- **Config drives everything.** Feeds, queries, company list, keywords,
  cadences, thresholds, and model names live in `config.yaml`. No URLs, search
  queries, or model IDs in code. `config.load_config()` also expands
  `companies.feeds` into `rss` sources named `company_<key>`.
- **No hardcoded models.** The scorer reads `models.scorer` from config. Never
  write a `claude-*` string in Python.
- **Network I/O is confined** to `ingest/http.py` (`get_text`, `get_json`),
  `PubMedSource.esearch/efetch` (Entrez), and `Scorer.create_message`
  (Anthropic). Tests monkeypatch those and never touch the network.
- **Tests use real saved feeds** in `tests/fixtures/` where a live sample could
  be captured; synthetic fixtures are used only for bot-protected endpoints
  (FDA OCE page, ClinicalTrials.gov API). Run `python -m pytest`.
- **Dedup order:** exact `dedup_hash` (sha256 of normalized title+url) -> DOI
  match -> near-duplicate normalized title (`difflib`, threshold in
  `dedup.title_similarity`). One cluster = one story; clusters keep the earliest
  `published_at`.
- **Every score row stores** `model`, `prompt_version` (`score/rubric.py:PROMPT_VERSION`),
  and the raw API response. Bump `PROMPT_VERSION` whenever the prompt, few-shot
  examples, or tool schema change; `run_score.py` then re-scores automatically.
- **Scoring is tool-use with a strict JSON schema** (`score/rubric.py:TOOL`).
  `total` is computed in code (`compute_total`), not by the model.
- **NCBI etiquette:** `Entrez.email`/`tool` from `config.ncbi`; rate limiter at
  3 req/s, or 10 req/s when `NCBI_API_KEY` is set in `.env`.
- **Cadence:** `run_ingest.py` only fetches sources whose last run (table
  `source_runs`) is older than `cadence_minutes`; `--force` overrides.
- **Logging:** stdlib `logging`. INFO for per-source counts, DEBUG for items.
- **Fail soft per source.** A source that errors is logged and recorded in
  `source_runs.error`; the run continues. `ingest/fda_oce.py` is an HTML scrape
  and returns `[]` on any failure.
- **Allowed dependencies:** anthropic, feedparser, biopython, httpx, pyyaml,
  python-dotenv, pydantic, pytest. Ask before adding anything else.
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
