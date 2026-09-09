# CLAUDE.md

Project guidance for Claude Code. Read PLAN.md before making changes.

## What this is
A human-in-the-loop pipeline that ingests immuno-oncology news, scores it with
the Anthropic API, and drafts X posts for human approval. The account is the
business and investing side of immuno-oncology biotech (CAR-T and cell therapy,
T-cell engagers and bispecifics, adjacent IO science): trial results and what
they mean, upcoming catalysts for public companies, M&A and financing. The AI
writes as a PhD-level immuno-oncology analyst at a hedge fund. Python 3.11+,
SQLite.
All seven build steps are implemented: 1 ingest + dedup + prefilter + score +
digest, 2 draft + human approval queue, 3 publish to X, 4 feedback loop,
5 operations (orchestrator, health, alerts, backups), 6 conference abstracts +
KOL X list + HTTP retry, 7 voice learning loop. The kickoff prompt that built
each step is in `prompts/` (see `prompts/README.md`). Nothing posts unless
`PUBLISH_ENABLED=1` **and** `--live`.

## Commands
- Install: `pip install -e ".[dev]"`
- Lint: `ruff check .` and `ruff format --check .`
- Test: `pytest`
- Run: `python run_ingest.py`, `python run_score.py`,
  `python digest.py [--rate] [--auto-rate]` (model ratings are stored as
  `rater='auto:<model>'`; human ratings stay the ground truth for tuning),
  `python run_draft.py`, `python run_verify.py` (claim checks with web search),
  `python run_queue.py` (approval UI on localhost:8000),
  `python run_publish.py` (dry run by default; `--live` needs `PUBLISH_ENABLED=1`),
  `python run_feedback.py snapshot|report|followers`,
  `python run_ops.py run|health|backup|status|prune` (cron orchestrator; see
  `ops/config.yaml` and `deploy/`)

## Rules
- **Config drives everything.** Feeds, queries, company list, keywords,
  cadences, thresholds, and model names live in `config.yaml`. Never hardcode
  queries, URLs, feeds, or `claude-*` model IDs in code. `config.load_config()`
  expands `companies.feeds` into `rss` sources named `company_<key>`,
  `conferences.meetings` into `crossref` sources `conf_<key>_abstracts` (plus
  `conf_<key>_news` rss when `news_rss` is set) and `kol` into one `x_list`
  source `kol_x_list`; an explicit `enabled:` is copied through expansion.
- **Network I/O is confined** to `ingest/http.py` (`get_text`, `get_json`;
  the only caller of `httpx.get` is its private `_request`, which retries
  429/5xx/transport errors and never logs headers),
  `PubMedSource.esearch/efetch` (Entrez), and `Scorer.create_message`
  (Anthropic), `draft/drafter.py:call_anthropic`, `score/rater.py:call_model`
  (the `digest.py --auto-rate` second-opinion rater), `claude_cli.run_claude` (the
  optional `models.backend: claude_code` path: the only place that spawns the
  Claude Code CLI; both Claude call sites route through it when selected, and
  the API stays the default), and `publish/client.py`
  (`post_tweet`, `verify_credentials`; the only place tweepy is imported, inside
  the functions). Tests monkeypatch those and never hit the network.
  `CrossrefSource.fetch_page` and `XListSource.fetch_page` are the single
  network methods of the step 6 sources (both call `http.get_json`).
- **Meeting windows:** `Source.is_due` honours `windows: [{start, end,
  cadence_minutes}]` (inclusive UTC dates); conference sources run hourly in a
  window and daily outside. Window dates in `config.yaml` are updated yearly.
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
- **Prefilter cap defers, never drops.** A cluster that passes the rules but
  hits `prefilter.daily_cap` keeps a NULL status and is retried next run; one
  older than `prefilter.max_age_days` is dropped as `stale`. After changing
  keywords run `run_score.py --refilter` (`db.reset_prefilter`) to re-evaluate
  earlier drops. `clusters.prefiltered_at` (guarded migration in `db.py`)
  is what the daily cap counts.
- **Fail soft per source.** Errors are logged and recorded in
  `source_runs.error`; the run continues. `ingest/fda_oce.py` returns `[]` on
  any failure.
- Read secrets from `.env` via python-dotenv; never commit `.env`.
- Logging: stdlib `logging`. INFO for per-source counts, DEBUG for items.
- Ask before adding a dependency not already in `pyproject.toml`.
- Generated content must never contain medical advice or investment advice
  (no buy/sell/hold/short calls, price targets, or return promises; describing
  a thesis, a valuation or a risk is fine). Preprints are labelled as
  preprints. `draft/drafter.py:check_hard_rules` enforces all of this in code
  after generation (plus 280 chars/post with URLs as 23, source URL placement,
  and verbatim-number verification); drafts that fail are stored as `failed`.
- Step 2 reads step 1's tables only through
  `approval_queue/store.py:fetch_candidates` (one candidate per cluster). Its own
  tables are `drafts` and `decisions`; edits log original vs edited text.
- Step 7 (voice learning) turns recent `decisions` into few-shot examples via
  `draft/examples.py` and builds the block once per `run_draft.py` run. Examples
  never override the hard rules: an edited text that fails `check_hard_rules` is
  never selected, the block sits before `HARD_RULES` in the system prompt, and
  every output is still checked in code. `draft/voice_report.py` only PROPOSES
  `voice.md` changes; a human edits `draft/voice.md` by hand. Settings live in
  `draft/config.yaml`; `decisions.category` is added by a guarded migration in
  `approval_queue/store.py:connect`; `draft_examples` records what each draft
  was shown. Step 7 reads `items` only through `fetch_decisions_for_voice` /
  `fetch_draft_stats` (source and url).
- Step 2b (`verify/`) checks `claims_to_verify` against the web. Its only
  network call is `verify/verifier.py:call_model` (CLI with
  `tools=["WebSearch","WebFetch"]`, the one caller that passes `tools` to
  `claude_cli.run_claude`; or the API `web_search` server tool). It owns
  `claim_checks`, reads drafts only through `approval_queue.store`, never edits a
  draft, and a verdict is `trusted` only for hosts in `verify/config.yaml` or
  company feed hosts. The queue blocks approve (409) on a contradicted claim
  unless `override=1`.
- Step 3 reads step 2's tables only through `publish/store.py:fetch_approved`
  (edited_text from `decisions` wins over `single_post`). Its own tables are
  `schedule` (claim row, one per draft) and `posts` (one row per tweet). Its
  settings live in `publish/config.yaml`, not the root config. Posting is
  idempotent via the claim; partial threads are never retried automatically.
  Texts are re-checked before posting and refused, never edited, on failure.
- Step 4 reads other steps' tables only through `feedback/store.py:fetch_posted`
  (posts) and `fetch_post_context` (drafts/decisions/items/scores/ratings). Its
  own tables are `tweet_metrics`, `follower_snapshots`, `feedback_reports`; its
  settings live in `feedback/config.yaml`. `feedback/client.py` is the only
  module that calls the X API (httpx, `X_BEARER_TOKEN`, read-only). Reports
  PROPOSE rubric/prefilter/slot changes; a human applies them and bumps
  `PROMPT_VERSION`. Analysis and suggestions are pure (no DB, no network).
- Step 5 (`ops/`) never imports another step's modules: `run_ops.py run`
  executes the other CLIs as subprocesses (order, timeouts, enabled/required in
  `ops/config.yaml`, which must never contain `--live`; a test asserts it) under
  an `fcntl` lock. It reads other steps' tables only through the read-only
  adapters in `ops/store.py` (each returns empty when a table is missing) and
  owns `pipeline_runs`, `health_checks`, `alerts_sent`. `ops/health.py` is pure
  (`now` is a parameter). The only network call in `ops/` is
  `alert.py:post_webhook` (plus `send_email` via smtplib); alerts carry check
  names, summaries and counts, never secrets or post text.
- Commit after each working module.

## Layout
```
ingest/   base.py (Item, Source ABC, windows), http.py (retry), rss.py,
          biorxiv.py, pubmed.py, clinicaltrials.py, fda_oce.py,
          crossref.py (conference abstracts), x_list.py (KOL list, read-only)
filter/   prefilter.py, dedup.py
score/    rubric.py, scorer.py, rater.py (second-opinion 1-5 rater)
db.py     sqlite: items, clusters, scores, ratings, source_runs
claude_cli.py  optional headless LLM backend (llm_backend, run_claude)
draft/    schema.py, prompt.py, voice.md, drafter.py, config.yaml, settings.py,
          examples.py (EditExample, select_edit_examples, format_examples_block),
          voice_report.py (VoiceReport, build_report, render_markdown, CLI)
approval_queue/  store.py (drafts, decisions, draft_examples, fetch_candidates,
          fetch_decisions_for_voice, fetch_draft_stats, record_examples),
          app.py (/voice), templates/
verify/   config.yaml, settings.py, verifier.py (ClaimCheck, verify_claim,
          call_model), store.py (claim_checks)
publish/  config.yaml, scheduler.py, thread.py, store.py (schedule, posts,
          fetch_approved), client.py
feedback/ config.yaml, models.py, analysis.py, suggest.py, report.py,
          store.py (tweet_metrics, follower_snapshots, feedback_reports,
          fetch_posted, fetch_post_context, due_for_snapshot), client.py
ops/      config.yaml, models.py, lock.py, runner.py, health.py, alert.py,
          backup.py, store.py (pipeline_runs, health_checks, alerts_sent +
          read-only adapters)
deploy/   crontab.example, pipeline.service, pipeline.timer, README.md
run_ingest.py  run_score.py  digest.py  run_draft.py  run_verify.py  run_queue.py
run_publish.py  run_feedback.py  run_ops.py   (CLIs)
```
