# Cancer Research X — AI-assisted pipeline

An AI pipeline that monitors oncology sources (PubMed, bioRxiv/medRxiv,
ClinicalTrials.gov, FDA, company PR), scores new items, and drafts posts for a
cancer-research X account. A human approves, edits, and adds commentary before
anything is published.

See [PLAN.md](PLAN.md) for the full design, principles, and build order.

## Principles

- Human-in-the-loop by default.
- Every post adds interpretation, never just description.
- No medical advice or treatment recommendations, ever.
- Always link the primary source; label preprints as preprints.
- Never fabricate numbers.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # then fill in ANTHROPIC_API_KEY and NCBI_EMAIL
```

## Development

```bash
ruff check .      # lint
ruff format .     # format
pytest            # tests
```

## Layout (planned)

```
ingest/       source fetchers (PubMed, bioRxiv, ClinicalTrials.gov, FDA, company PR)
score/        scoring rubric + Anthropic API scorer
db.py         SQLite storage with dedup
config.yaml   queries, feeds, thresholds
tests/        pytest, network calls mocked
```

## Status

Step 1 (ingest + score) is next. See the kickoff prompt at the bottom of PLAN.md.
