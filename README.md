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
cp .env.example .env        # add ANTHROPIC_API_KEY (and optionally NCBI_API_KEY)
ruff check . && ruff format --check . && pytest
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
python run_draft.py             # draft approved candidates
python run_queue.py             # approval UI on localhost:8000
python run_publish.py           # DRY RUN (default): print what would post and when
python run_publish.py --live    # posts only if PUBLISH_ENABLED=1 is also set
python run_publish.py --live --breaking   # only FDA / company-approval items
python run_publish.py --live --now        # ignore slots, post the top candidate once
```

Suggested cron: `run_ingest.py` every 30 min, `run_score.py` hourly, read
`digest.py` daily, `run_publish.py --live` every 15 min.

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

## Publishing (step 3)

`run_publish.py` reads approved drafts through `publish/store.py:fetch_approved`
and posts them to X via tweepy (`publish/client.py`, the only module that
imports tweepy). Slots, timezone, daily cap, minimum gap between posts and
the breaking-news rules live in `publish/config.yaml`.

Safety gates, all of which must hold before a single tweet is sent:

- `--live` is passed **and** `PUBLISH_ENABLED=1` is set. Anything else is a
  dry run that prints the plan and posts nothing.
- The draft is claimed in a `BEGIN IMMEDIATE` transaction (table `schedule`)
  before the API call, so two overlapping cron runs cannot post it twice and a
  draft is never retried after a failure.
- Every text is re-checked in code right before posting (<= 280 chars with
  URLs as 23, source URL in the single post / last thread post). Failures are
  logged as refusals and go back to the approval queue; nothing is auto-fixed.
- Breaking items (`fda*` sources, `company_*` PRs whose title mentions an
  approval) may post outside slots but still respect the daily cap and gap.
- A thread that fails at post k keeps posts 1..k-1 live, records the error on
  post k, marks the draft `partial`, and stops. It is not retried; a human
  finishes or deletes it.

### Bio disclosure (manual)

PLAN.md requires the account bio to disclose AI-assisted drafting. Editing
the bio is deliberately **not** automated. Before the first live run, edit the
bio on X by hand, then set `BIO_DISCLOSURE_CONFIRMED=1` in `.env`;
`run_publish.py` logs a warning at startup until it is set.

## Database

SQLite (`db_path` in config). Tables: `items`, `clusters`, `scores`,
`ratings`, `source_runs` (step 1); `drafts`, `decisions` (step 2); `schedule`,
`posts` (step 3). Every score is kept, so re-scoring after a prompt
change is additive.

## Known source caveats

- EurekAlert no longer publishes RSS; bioRxiv/medRxiv RSS was retired, so
  those use the `api.biorxiv.org` JSON API.
- `fda.gov` (OCE approvals page) and `clinicaltrials.gov` sit behind bot
  protection that blocks some cloud egress. Both sources fail soft; run from a
  residential/VPS IP or add a proxy if they return 401/403.
- Several big-pharma newsrooms have no public feed or block bots; the company
  list in `config.yaml` contains only feeds verified to work.

## Conference abstracts and KOL list (step 6)

Two more ingest sources plus retry in the HTTP layer. Nothing here calls the
Anthropic API or writes to X.

**HTTP retry.** `ingest/http.py` retries 429 and 5xx responses and transport
errors (connection failures, timeouts) up to three attempts with exponential
backoff, honours a numeric `Retry-After` (capped at 60 s), and never retries
other 4xx. The DEBUG log line prints the URL and query only, never headers.

**Meeting windows.** Any source may carry
`windows: [{start, end, cadence_minutes}]`; on days inside a window
(inclusive) that cadence replaces `cadence_minutes`. `config.yaml` uses this
to run the conference sources hourly from abstract release through the
meeting and daily otherwise. The window dates must be updated every year from
the society pages linked in `config.yaml`.

**Conference abstracts (`type: crossref`).** Societies publish their meeting
abstracts as journal supplements (JCO for ASCO, Cancer Research for AACR,
Blood for ASH, Annals of Oncology for ESMO) that Crossref indexes under the
journal's ISSN. `conferences.meetings` expands into `conf_<key>_abstracts`
sources that query `api.crossref.org/works` by ISSN and created date, keep
only works whose issue or DOI matches `issue_pattern`, gate on
`prefilter.allow_keywords`, put late-breaking / plenary abstracts first, then
newest, and cap each run at `max_items_per_run`. Abstracts get one factual
line prepended (`ASCO Annual Meeting 2026 abstract (Journal of Clinical
Oncology 44, 16_suppl).`); titles are untouched so the later full paper joins
the same cluster by DOI or title. A meeting's `news_rss` becomes
`conf_<key>_news` with the same windows. ESMO is shipped `enabled: false`:
Elsevier deposits the Annals of Oncology abstract book in Crossref after the
congress, without abstracts. Set `CROSSREF_MAILTO` in `.env` (or
`crossref.mailto`) to use Crossref's polite pool.

The flood of a meeting week is handled inside the source (issue filter,
keyword gate, priority order, per-run cap). Prefilter and scoring policy are
unchanged; if the daily cap in `prefilter.daily_cap` turns out to be the
binding constraint during ASCO, raise it for the window rather than changing
the prefilter code.

**KOL X list (`type: x_list`, disabled).** `kol` expands into one read-only
source, `kol_x_list`, that reads recent posts from a single X list via
`GET /2/lists/:id/tweets` with app-only bearer auth. To turn it on:

1. Create the list by hand on X from the accounts documented under
   `kol.handles` (public professional accounts only) and put its id in
   `X_KOL_LIST_ID`.
2. Reading a list needs X API read access, which is a paid tier. The bearer
   token (`X_BEARER_TOKEN`) is shared with step 4's feedback snapshots, so the
   read budget is shared too; `kol.monthly_request_cap` is this source's
   share and a test checks `cadence_minutes` x `max_pages` stays under it.
3. Set `kol.enabled: true`. Until then the source is listed but never runs; if
   it is enabled without a token or list id it fails soft with a clear error
   in `source_runs.error`.

Retweets and replies are skipped, posts without an off-platform link are
skipped, t.co links are replaced by their expanded URLs, and a post that links
a DOI joins that paper's cluster. `lookback_hours` must exceed
`cadence_minutes` so consecutive runs overlap; the dedup hash makes the
overlap harmless. The token is never logged.
