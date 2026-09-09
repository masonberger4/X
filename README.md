# Cancer research news pipeline — step 1

Ingest, dedup, prefilter, score, and digest cancer-research news from journals,
preprint servers, PubMed, ClinicalTrials.gov, FDA, and company press releases.
See `PLAN.md` for the full roadmap; this repo implements step 1 only (no
drafting or posting).

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env        # add ANTHROPIC_API_KEY (and optionally NCBI_API_KEY)
python -m pytest
```

Edit `config.yaml` to change feeds, PubMed queries, company list, keywords,
cadences, the score threshold, or the scoring model.

## Run

```bash
python run_ingest.py            # fetch every source that is due by cadence
python run_ingest.py --force    # ignore cadence
python run_ingest.py --source pubmed_oncology -v
python run_score.py             # prefilter + score unscored clusters
python run_score.py --dry-run   # see what would be scored
python digest.py                # top-N clusters of the last 24h as markdown
python digest.py --all --hours 72 --out digest.md
python digest.py --rate         # rate each entry 1-5 with a note (saved to `ratings`)
```

Suggested cron: `run_ingest.py` every 30 min, `run_score.py` hourly, read
`digest.py` daily.

## How it works

1. **Ingest** (`run_ingest.py`): each source in `config.yaml` has a `type`
   (`rss`, `biorxiv`, `pubmed`, `clinicaltrials`, `fda_oce`) and a
   `cadence_minutes`. Items are normalised into `Item` (pydantic) with a
   `dedup_hash` of normalised title+url.
2. **Dedup / cluster** (`filter/dedup.py`): exact hash -> DOI -> near-duplicate
   title. One cluster per story; a cluster records every source that covered it.
3. **Prefilter** (`filter/prefilter.py`): keyword allow/deny, short-abstract
   drop, daily cap. Cheap and deterministic; runs before any API call.
4. **Score** (`score/`): batches of ~10 clusters go to the model named in
   `models.scorer` via tool use with a strict JSON schema. Each row stores the
   model, prompt version, raw response, five 0-10 dimensions, evidence level,
   hype risk, rationale and suggested angle. `total` = sum of dimensions minus
   half the hype risk (0-50).
5. **Digest** (`digest.py`): top clusters above `scoring.threshold` in the
   window, as markdown. `--rate` collects human ratings for rubric tuning.

## Database

SQLite (`db_path` in config). Tables: `items`, `clusters`, `scores`,
`ratings`, `source_runs`. Every score is kept, so re-scoring after a prompt
change is additive.

## Known source caveats

- EurekAlert no longer publishes RSS; bioRxiv/medRxiv RSS was retired, so
  those use the `api.biorxiv.org` JSON API.
- `fda.gov` (OCE approvals page) and `clinicaltrials.gov` sit behind bot
  protection that blocks some cloud egress. Both sources fail soft; run from a
  residential/VPS IP or add a proxy if they return 401/403.
- Several big-pharma newsrooms have no public feed or block bots; the company
  list in `config.yaml` contains only feeds verified to work.
