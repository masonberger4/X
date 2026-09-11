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
KOL X list + HTTP retry, 7 voice learning loop, 8 control panel (one web app over
the whole workflow). The kickoff prompt that built
each step is in `prompts/` (see `prompts/README.md`). Nothing posts unless
`PUBLISH_ENABLED=1` **and** `--live`.

## Commands
- Install: `pip install -e ".[dev]"`
- Lint: `ruff check .` and `ruff format --check .`
- Test: `pytest`
- Run: `python run_ingest.py`, `python run_score.py`,
  `python digest.py [--rate] [--auto-rate]` (the editor answers yes/no per story plus
  a required explanation that starts with a reason category from
  `score/editorial.py`; stored in `ratings` as 5/1, and the model rater answers the
  same question as `rater='auto:<model>'`; human decisions stay the ground truth),
  `python run_draft.py`, `python run_verify.py` (claim checks with web search),
  `python run_queue.py` (approval UI on localhost:8000),
  `python run_app.py` (control panel: dashboard, sources, runs and the queue, same port),
  `pythonw run_desktop.py` (the panel in a native window; `pyinstaller deploy/desktop.spec`
  builds `dist/Pipeline/` with `Pipeline.exe` + `pipeline-cli.exe`; both need the
  `desktop` extra), `python pipeline_cli.py <run_x.py> ...` (the CLIs behind one entry point),
  `python run_publish.py` (dry run by default; `--live` needs `PUBLISH_ENABLED=1`),
  `python run_feedback.py snapshot|report|followers`,
  `python run_ops.py run|health|backup|status|prune` (cron orchestrator; see
  `ops/config.yaml` and `deploy/`), `python run_logos.py [--only KEY] [--force] [--dry-run]`
  (operator command: each configured company's own site icon into `assets/logos/`)

## Rules
- **Config drives everything.** Feeds, queries, company list, keywords,
  cadences, thresholds, and model names live in `config.yaml`. Never hardcode
  queries, URLs, feeds, or `claude-*` model IDs in code. `config.load_config()`
  expands `companies.feeds` into `rss` sources named `company_<key>`,
  `conferences.meetings` into `crossref` sources `conf_<key>_abstracts` (plus
  `conf_<key>_news` rss when `news_rss` is set) and `kol` into one `x_list`
  source `kol_x_list`; an explicit `enabled:` is copied through expansion. A source may set its own
  `user_agent` (`Source.user_agent()` falls back to `http.user_agent`); the ClinicalTrials.gov
  source must keep a `python-httpx/` token in it, since that host's firewall rejects a
  Python client claiming to be a browser.
- **Network I/O is confined** to `ingest/http.py` (`get_text`, `get_json`, `get_bytes`;
  the only caller of `httpx.get` is its private `_request`, which retries
  429/5xx/transport errors and never logs headers),
  `PubMedSource.esearch/efetch` (Entrez), and `Scorer.create_message`
  (Anthropic), `draft/drafter.py:call_anthropic`, `score/rater.py:call_model`
  (the `digest.py --auto-rate` second-opinion rater, and the call behind
  `filter/link.py` story linking), `draft/grader.py:call_grader` (the image
  grader: the PNG as an image block, or the CLI with `tools=["Read"]`),
  `claude_cli.run_claude` (the
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
  `published_at`. **Story linking** (`filter/link.py`, run by `run_score.py`
  after the prefilter, `--no-link` skips it): one call to `models.linker` over
  the passed clusters of the last `linking.window_hours` (scored or not)
  proposes same-event groups; code validates them (unknown ids, overlaps,
  singletons dropped) and `db.merge_clusters` folds each group into the
  cluster that already has a score, else the oldest, moving items, scores and
  ratings. It never raises: a failed call is logged and scoring proceeds.
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
- **Draft images** (`draft/chart.py`): the drafter's optional `chart` is a bar-chart
  SPEC (title, labels, values, unit, note), never a picture; `suggested_visual` stays a
  text hint for the reviewer. `drafter.verify_chart` checks every number in it verbatim
  against the source and `drop_unverified_chart` drops the chart (text untouched, a
  low-confidence claim says why) on one miss. `run_draft.py` and the queue's revise
  route render the survivor through `approval_queue/images.py:attach_chart` (matplotlib,
  the `images` extra, imported inside `render_chart`; fail-soft: no image, never no
  draft) to `<db folder>/images/draft_<id>.png` (`store.image_dir()`); `drafts.chart_json`
  and `drafts.image_path` are guarded migrations. `images.enabled` in `draft/config.yaml`
  turns rendering off. The queue serves it at `/drafts/{id}/image` and `store.drop_image`
  is the only way a human removes it. Step 3 attaches it to the FIRST post
  (`publish/client.py:upload_media`, v2 media/upload + media/metadata, then POST /2/tweets with
  `media_ids`); `media.attach_images` in `publish/config.yaml` turns that off, and an upload
  failure posts nothing and marks the draft `failed`.
- **Draft tables** (`draft/chart.py:Table`, the alternative to a chart; `Draft.visual` is
  whichever is set, both stored in `chart_json` with `kind: table` for a table): cells may
  go beyond the source, so `attach_chart` never renders one. `run_verify.py`'s table pass
  (`verify/tables.py`, pure) marks cells verbatim in the article as supported
  (`model='source'`), sends every other cell to `verify_claim` as
  `"<row label>, <column>: <cell>"`, stores verdicts in `table_checks` (verify's second
  table), and once every cell has one either renders through
  `approval_queue/images.py:attach_table` with unsupported cells blanked, or drops the
  table via `store.drop_table` (an `edit` decision carrying the reason) on a contradicted
  cell, too few supported cells (`tables.min_supported_ratio`), fewer than two verified row
  labels, or more than `tables.max_cells_per_draft` cells. This is the one place step 2b
  changes a draft row, and it never touches the text. The queue's approve route drops a
  table whose picture was not rendered yet; revise calls
  `verify/store.py:carry_over_table_checks` (same row label, column and cell text keeps
  its verdict). `check_hard_rules` scans table cells for advice phrases.
- Step 2 reads step 1's tables only through
  `approval_queue/store.py:fetch_candidates` (one candidate per cluster). Its own
  tables are `drafts` and `decisions`; edits log original vs edited text.
  A human asks for changes in words, not by retyping: `POST /drafts/{id}/revise`
  calls `draft/drafter.py:revise_item` (same `call_anthropic`, same schema check and
  `check_hard_rules` loop as `draft_item`; the user prompt is
  `draft/prompt.py:build_revision_user_prompt`) with the current draft, the
  instructions and every step 2b check that is contradicted or unverified. The result
  replaces the whole draft via `store.revise` (status unchanged, a `revise` decision
  holds the before/after, `note` is the instruction), then the route calls
  `verify/store.py:carry_over_checks`: a `supported` verdict whose claim text is unchanged
  (up to case, spacing, trailing full stop) is re-indexed and kept, every other
  `claim_checks` row is dropped. On any failure the draft is untouched. `revise` decisions are never
  few-shot examples (the AFTER text is not human-written); publish honours the latest
  `edit` or `revise` text.
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
  draft's text itself, and a verdict is `trusted` only for hosts in `verify/config.yaml` or
  a company's own site (`verifier.trusted_hosts`: each `companies.feeds` URL host and
  `domain:`, plus every `branding.companies` `domain:`). `run_verify._decide` re-derives
  trust from each stored verdict's source URL against the current host list, so adding a
  company to config makes its checked cells count without a new web call. The one exception is the **verify-revise loop**
  (`verify/autorevise.py`, `run_verify.py --auto-revise` or `auto_revise.enabled` in
  `verify/config.yaml`, on in the shipped config; `--no-auto-revise` skips a run): after
  the claim pass, a draft with a
  contradicted or unverified claim is revised through the queue's own path
  (`drafter.revise_item` with `claim_problems` and no instructions, `store.revise` with
  note `autorevise.AUTO_NOTE`, `carry_over_checks`), its new claims are checked, and the
  round repeats until every claim is supported or `max_rounds` (per run) /
  `max_rounds_per_draft` (per draft, counted from `revise` decisions with that note;
  0, the shipped value, means no lifetime cap) is
  hit. A revision whose claim set is unchanged is discarded; a draft with an unchecked
  claim is never revised. Tables are outside the loop. `claim_problems` lives there and
  the queue app imports it. The queue blocks approve (409) on a contradicted claim
  unless `override=1`.
- Step 3 reads step 2's tables only through `publish/store.py:fetch_approved`
  (edited_text from `decisions` wins over `single_post`; it also resolves the draft's
  image path and alt text). Its own tables are
  `schedule` (claim row, one per draft) and `posts` (one row per tweet). Its
  settings live in `publish/config.yaml`, not the root config. Posting is
  idempotent via the claim; partial threads are never retried automatically.
  The queue reads those two tables back only through
  `approval_queue/store.py:publish_states` (read-only, empty when the tables are
  missing) to label and hide posted drafts on the approved page.
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
- Step 8 (`panel/`) owns no tables, no config file of its own and no pipeline
  logic. It reads other steps only through `ops/store.py`'s read-only adapters plus
  `run_ops.build_report`; the one exception is `panel/feed.py`, which uses step 1's own
  `db.Database` API (as `digest.py` does) to list scored clusters and to write a human
  yes/no decision — the only row the panel writes outside its own pages, and it opens that
  Database inside the route because a `Database` keeps its connection to one thread.
  It renders through the pure functions in `panel/views.py`
  (`now` is a parameter; no DB, network or clock), and includes the step 2 queue's
  routes into the same app so the queue's own module stays unchanged apart from its
  index moving to `/queue`. `panel/jobs.py` never builds an argv: a job names steps
  from `ops/config.yaml` and `ops/runner.py` runs them under `ops/lock.py`, one job
  at a time, refusing any step whose argv contains `--live`. `JobManager.cancel()` ends a run:
  `ops/runner.terminate_active()` kills the live step's process tree (own process group on
  POSIX, `taskkill /T` on Windows) and the remaining steps are skipped as `cancelled`; the
  runs page's Stop button and `run_desktop.py` closing both call it. The panel has no publish
  button and never writes `config.yaml`, `draft/voice.md` or a draft's text. It has
  no authentication: `run_app.py` binds localhost by default. `/publishing` and
  `/feedback` are views: no post button, and a report's suggestions are rendered, never
  applied. The desktop build (`run_desktop.py`, `pipeline_cli.py`, `deploy/desktop.spec`)
  changes no step: `panel/frozen.py` decides the data dir (exe folder when frozen, else
  the repo root), the step interpreter (`pipeline-cli` when frozen) and the bundle
  manifest (every `*/config.yaml`, `draft/voice.md`, both template dirs, each CLI script
  as a marker for `ops/runner.py:cli_missing`, which also looks in `sys._MEIPASS`).
  `pipeline_cli.py` dispatches only the names in `panel/frozen.py:CLIS`. pywebview and
  PyInstaller live in the `desktop` extra only.
- **Docs move with the code.** `tests/test_docs_coverage.py` fails when a CLI,
  a `--flag`, an `ops/config.yaml` step or a settings file is not named in
  HOWTO.md / README.md (flags may instead sit in the CLI's usage docstring),
  and the CI `docs` job fails a PR that touches operator-facing files without
  touching a doc, unless the PR description says `docs-not-needed`. A change in
  what the operator runs, sees or configures updates HOWTO.md in the same PR.
- Commit after each working module.

## Layout
```
ingest/   base.py (Item, Source ABC, windows), http.py (retry), rss.py,
          biorxiv.py, pubmed.py, clinicaltrials.py, fda_oce.py,
          crossref.py (conference abstracts), x_list.py (KOL list, read-only)
filter/   prefilter.py, dedup.py, link.py (story linking: same-event groups -> one cluster)
score/    rubric.py, scorer.py, editorial.py (yes/no decision, reason categories),
          rater.py (second-opinion yes/no rater)
db.py     sqlite: items, clusters, scores, ratings, source_runs
claude_cli.py  optional headless LLM backend (llm_backend, run_claude)
draft/    schema.py, chart.py (chart + table specs, verification, PNG rendering, Style
          knobs, 3D header, logos), grader.py (image grader: ImageGrade, CHECKLIST,
          grade_image, call_grader), branding.py (tickers + logos for company cells),
          logos.py (site icon discovery + PNG normalisation for run_logos.py), prompt.py,
          voice.md, drafter.py, config.yaml, settings.py,
          examples.py (EditExample, select_edit_examples, format_examples_block),
          voice_report.py (VoiceReport, build_report, render_markdown, CLI)
approval_queue/  store.py (drafts, decisions, draft_examples, fetch_candidates,
          fetch_decisions_for_voice, fetch_draft_stats, record_examples, image_dir,
          set_image, drop_image, image_grades), images.py (attach_chart, attach_table,
          render-grade loop), app.py (/voice,
          /drafts/{id}/image), templates/
panel/    views.py (pure view models, sparkline geometry), feed.py (scored feed +
          ratings), jobs.py (JobManager, background step runs), frozen.py (data dir,
          step interpreter and bundle manifest for the desktop build),
          app.py (dashboard, /sources, /feed, /runs, /publishing, /feedback), templates/
verify/   config.yaml, settings.py, verifier.py (ClaimCheck, verify_claim,
          call_model), store.py (claim_checks, table_checks), tables.py (cell claims,
          source-backed cells, the render/drop decision)
publish/  config.yaml, scheduler.py, thread.py, store.py (schedule, posts,
          fetch_approved), client.py (post_tweet, upload_media, verify_credentials)
feedback/ config.yaml, models.py, analysis.py, suggest.py, report.py,
          store.py (tweet_metrics, follower_snapshots, feedback_reports,
          fetch_posted, fetch_post_context, due_for_snapshot), client.py
ops/      config.yaml, models.py, lock.py, runner.py, health.py, alert.py,
          backup.py, store.py (pipeline_runs, health_checks, alerts_sent +
          read-only adapters)
assets/   logos/<company key>.png (human-supplied company logos for table cells)
deploy/   crontab.example, pipeline.service, pipeline.timer, desktop.spec, README.md
run_ingest.py  run_score.py  digest.py  run_draft.py  run_verify.py  run_queue.py
run_app.py  run_desktop.py  pipeline_cli.py  run_publish.py  run_feedback.py  run_ops.py
run_logos.py   (CLIs)
```
