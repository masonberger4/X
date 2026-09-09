# CLAUDE.md

Project guidance for Claude Code. Read PLAN.md before making changes.

## What this is
A human-in-the-loop pipeline that ingests oncology news, scores it with the
Anthropic API, and drafts X posts for human approval. Python 3.11+, SQLite.

## Commands
- Install: `pip install -e ".[dev]"`
- Lint: `ruff check .` and `ruff format --check .`
- Test: `pytest`

## Rules
- Never hardcode queries, URLs, or feeds in code; they live in `config.yaml`.
- All network I/O goes behind small functions so tests can mock it. Tests never hit the network.
- Read secrets from `.env` via python-dotenv; never commit `.env`.
- Respect NCBI etiquette: set Entrez.email from config, max 3 requests/s.
- Ask before adding a dependency not already in `pyproject.toml`.
- Do not build posting or X API integration until steps 1 and 2 are done.
- Generated content must never contain medical advice. Preprints are labelled as preprints.
