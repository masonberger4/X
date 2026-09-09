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
