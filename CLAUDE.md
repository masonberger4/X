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
the whole workflow), 9 swarm drafting (phase one: many cheap cells + layers + jury
against the single strong drafter; phase two: X fitness, round-robin seed genomes and
pruning via `run_evolve.py`; phase three: breeding of writer genomes by one strong call
and of designer genomes, the picture's starting `Style`, by a random knob step, and the
panel's `/swarm` page; phase four: the format itself, thread or single or long post and how
many pictures on which posts, is a third bred population). The kickoff prompt that built
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
  `python run_draft.py` (`--no-swarm` for the single drafter only), `python run_verify.py`
  (claim checks with web search),
  `python run_queue.py` (approval UI on localhost:8000),
  `python run_app.py` (control panel: dashboard, sources, runs and the queue, same port;
  the run buttons sit on the pages they affect and "Publish now" lives on the approved page),
  `pythonw run_desktop.py` (the panel in a native window; `pyinstaller deploy/desktop.spec`
  builds `dist/Pipeline/` with `Pipeline.exe` + `pipeline-cli.exe`; both need the
  `desktop` extra), `python pipeline_cli.py <run_x.py> ...` (the CLIs behind one entry point),
  `python run_publish.py` (dry run by default; `--live` needs `PUBLISH_ENABLED=1`;
  `--draft ID` targets one approved draft),
  `python run_feedback.py snapshot|report|followers`,
  `python run_evolve.py [score|prune|breed|report] [--dry-run] [--force]` (step 9: swarm
  fitness from X and pruning, no network; `breed` makes the one strong-model call per
  writer child),
  `python run_scrub_notes.py [--status STATUS] [--dry-run] [-v]` (operator command: blanks
  picture captions written to the operator in queued drafts and redraws them),
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
- **Times are stored in UTC and shown in one zone.** Every timestamp in SQLite stays
  an aware-UTC ISO string and every comparison, window and API payload stays UTC;
  conversion happens only when a datetime becomes text for a human. `timeutil.py` is the
  single place that converts (`timezone_name`, `display_tz`, `to_display`,
  `fmt_datetime` -> "2026-06-01 08:30 PDT", `fmt_date`, `install_jinja_filters` ->
  the Jinja filters `|localtime` / `|localdate`) and the only reader of the root
  `config.yaml` key `timezone:` (`America/Los_Angeles`). Converted: the panel's
  dashboard/publishing/feedback pages, the queue's draft detail and voice pages,
  `panel/feed.py`, `digest.py`, `run_ops.py status`, `ops/health.py`'s report heading,
  `ops/alert.py`'s alert body, `feedback/report.py`'s heading, `draft/voice_report.py`'s
  window line and edit headings. Still UTC on purpose, as sort/parse keys: backup
  filenames `backups/pipeline-<UTC stamp>.sqlite`, the `run_id` stamps and
  `feedback/store.py:day_of`'s `captured_on` bucket; relative ages are zone-independent.
  `publish/config.yaml` and `feedback/config.yaml` keep their own `timezone:` because
  those drive behaviour (posting slots, the "hour posted" column), not display; all
  three are set to the same zone.
- Read secrets from `.env` via python-dotenv; never commit `.env`.
- Logging: stdlib `logging`. INFO for per-source counts, DEBUG for items.
- Ask before adding a dependency not already in `pyproject.toml`.
- Generated content must never contain medical advice or investment advice
  (no buy/sell/hold/short calls, price targets, or return promises; describing
  a thesis, a valuation or a risk is fine). Preprints are labelled as
  preprints. `draft/drafter.py:check_hard_rules` enforces all of this in code
  after generation (plus 280 chars/post with URLs as 23, source URL placement,
  and verbatim-number verification); drafts that fail are stored as `failed`.
- **Mentions and hashtags are a hard rule** (rule 11 in `draft/prompt.py:hard_rules`,
  mirrored by `draft/tags.py:tag_problems`, called from `check_hard_rules` per post and
  from `swarm/cells.py:cell_problems` per cell): an account whose X handle the story is
  given (`config.yaml`: `x:` on a `companies.feeds` / `branding.companies` entry, and the
  `mentions:` list of journals, societies and regulators with `domains:` and
  `match_names:`) must be written as @handle when a post names it, and a formal drug name
  (INN stem regex, `-cel` short names) or trial name (`KEYNOTE-189` shape) must be a
  hashtag; a configured company name (`tags.company_names`, `drafter.known_company_names`)
  is never a drug, so Genmab is not `#Genmab`. Handles are never guessed: `drafter.story_handles` (`tags.load_handles` +
  `relevant_handles`: named in the source text, owning the URL host, or the
  `company_<key>` source) is the only list the model sees (`X HANDLES` in the user prompt,
  `Brief.handles` for the swarm) and the only one enforced. `numbers_in` ignores
  `@`/`#` tokens so a trial name's digits are not a number to verify. Publish's re-check
  and human-approved texts are untouched.
- **The first post is the hook** (`draft/hook.py`, pure; rule 12 in `draft/prompt.py:hook_rule`,
  enforced by `hook_problems` from `check_hard_rules` on `thread[0]` and from
  `swarm/cells.py:cell_problems` on a hook cell). It carries no link of any kind, no thread
  position marker ("1/6"), no "thread" and no emoji, and it stays within `HOOK_MAX_CHARS`:
  X ranks a thread on its opening post, and an outbound link or an unanswerable summary
  there costs the rest of the thread its readers. The source URL stays in the last post
  (rule 2). A single or long post is its own opener and is NOT exempt from the link
  ban: its primary source URL goes in a second, threaded post (the **link post**,
  `hook.py:link_post_problems`, `LINK_POST_MAX_CHARS`), so `draft/schema.py:Format`
  normalises every non-thread shape to exactly two posts (`LINK_POST_SHAPE_POSTS`), the
  picture never anchors to the link post, no swarm cell carries a link
  (`engine.run_swarm`'s `link_post`), and `publish/thread.py:split_thread(number=False)`
  leaves the pair unnumbered. Only the hook's length cap is lifted there (the format's
  `max_chars` applies). A one-post draft stored before this rule still passes
  (`carries_url=True`).
- **KPIs are weighted, not counted.** `feedback/models.py:CONVERSATION_WEIGHTS` defines the
  derived `conversation` KPI (reply/quote x3, bookmark/repost x2, like x1) beside the six
  stored counts; `Metrics.get` and `swarm/store.py:_metrics` both serve it, and it is the
  shipped `kpi:` in `feedback/config.yaml` and `evolve.kpi` in `swarm/config.yaml`, so the
  report and the swarm's selection point at conversation rather than at reach. `swarm/`
  imports `feedback.models` for those weights only (pure dataclasses, no DB, no network).
- **The shape of a draft is a gene, not a constant** (step 9 phase four). `Draft.thread`
  is always the list of posts; `draft/schema.py:Format` (shape `thread` | `single` |
  `long`, `min_posts`/`max_posts`, `visuals` 0-2, one anchor word per visual, `max_chars`)
  says what `validate_output(data, fmt)` and `check_hard_rules(..., fmt)` require, and
  with `fmt=None` they require the phase-one physics: a 3-6 post thread with exactly one
  of `chart`/`table`. A single post is a one-element thread of 280 chars; a long post is
  one element of up to `formats.long_max_chars` (`swarm/config.yaml`, a Premium long-form
  post) written as sections. The first visual may be a chart or a table; a further one
  (`visuals` in the output, `Draft.extra_visuals`) must be a chart. `Draft.shape`,
  `anchors` (1-based post per visual), `max_chars` and `wanted_visuals` are stored in
  `drafts.format_json` (guarded migration; `drafter.format_of` rebuilds the Format for a
  revision) and every rendered picture in `drafts.images_json` (`store.set_image(...,
  index=k)`, `image_file(id, k)`; `image_path`/`image_alt` stay the first picture).
- **Draft images** (`draft/chart.py`): the drafter's `chart` is a bar-chart
  SPEC (title, labels, values, unit, note), never a picture; `suggested_visual` stays a
  text hint for the reviewer. `drafter.verify_chart` checks every number in it verbatim
  against the source and `drafter.chart_problems` turns one miss into a retry reason
  (never a silent drop; a draft that never gets it right is stored `failed`).
  `run_draft.py` and the queue's revise route render the chart through `approval_queue/images.py:attach_chart` (matplotlib,
  the `images` extra, imported inside `render_chart`; fail-soft: no image, never no
  draft) to `<db folder>/images/draft_<id>.png` (`store.image_dir()`); `drafts.chart_json`
  and `drafts.image_path` are guarded migrations. `images.enabled` in `draft/config.yaml`
  turns rendering off. **A note is a caption, not an aside**: a chart's or table's `note` is
  printed under the picture, so `chart.py:note_problems` (called from `check_hard_rules` for
  both) fails a draft whose note addresses the operator ("verify each cell before posting",
  "TODO") and the attempt is retried. `run_scrub_notes.py` is the one-off pass over
  queued drafts made before that rule: `store.clear_visual_notes` blanks the caption (an
  `edit` decision, text unchanged) and the picture is drawn again. **Colour is a knob, not a constant**: `draft/chart.py:PALETTES` holds
  the named palettes and `Style.palette` / `Style.multi_colour` pick one, so the designer
  genome and the image grader (`draft/grader.py`, whose knob list and checklist name them;
  `distinctiveness` replaced the old house-style row) both vary it; `Style.apply` ignores an
  unknown palette. Company bars in a chart are branded like table cells
  (`branding.brand_chart` from `images.attach_chart`: ticker in the label, logo in the
  gutter, `render_chart(logos=)`); `brand_table` also tries the row-label column. The queue serves it at `/drafts/{id}/image` and `store.drop_image`
  is the only way a human removes it (every picture at once). Step 3 attaches each picture
  to the post it is anchored to, the first post before phase four (`publish/store.py`
  reads `format_json`/`images_json` into `Approved.shape`, `max_chars`, `images`;
  `run_publish.images_for` / `publish_one(images=)`; `publish/thread.py` checks each post
  against the draft's `max_chars`; `publish/client.py:upload_media`, v2 media/upload +
  media/metadata, then POST /2/tweets with `media_ids`); `media.attach_images` in
  `publish/config.yaml` turns that off, an upload failure before post 1 posts nothing and
  marks the draft `failed`, a later one leaves a partial thread.
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
  tables are `drafts`, `decisions`, `draft_examples` and `image_grades`; edits log original vs edited text.
  An approve is reversible: `POST /drafts/{id}/reopen` (`store.reopen`, a `reopen`
  decision carrying the text and the optional note) puts an approved draft back to
  `pending`. `approval_queue/publishing.py` is the queue's one door to step 3 (as
  `panel/publishing.py` is the panel's): `block_reason` refuses the reopen when
  `publish/store.py:is_live` finds a `posts` row with a tweet id — the ground truth,
  asked before and independently of the schedule — or when the draft's
  `store.publish_states` entry is not `PublishInfo.reopenable`
  (`store.REOPENABLE_STATES`, an allowlist of `pending`/`failed`/`refused`, which both
  templates read too so the button and the route cannot drift). Nothing on X is ever
  unposted here. The status flips first, which hides the draft from `fetch_approved` so
  no run can claim it, and only then does `publishing.forget` call
  `publish/store.py:forget` (step 2's one write into step 3's tables: it deletes that
  draft's `schedule` row when it was never claimed, or claimed and failed or refused,
  and never one that is posted or partial), so a saved `position` cannot resurrect
  itself on re-approval. There is no snooze: a draft left `snoozed` in an older database
  is migrated to `pending` by `store.connect`, and `drafts.snoozed_until` stays in the
  schema as a dead column so old databases need no rebuild.
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
  company to config makes its checked cells count without a new web call. The render/drop
  step itself lives in `verify/render.py:finalize_table` (the one module in `verify/` that
  writes the picture), shared with the queue's **trust button**: `POST /drafts/{id}/trust`
  beside an "(untrusted source)" verdict calls `verify/settings.py:add_trusted_domain` (a
  line edit of `trusted_domains` in `verify/config.yaml`, comments kept; the one key the
  queue writes in any settings file), `verify/store.py:mark_host_trusted` (flips `trusted`
  on stored verdicts from that host, verdicts untouched) and, for a pending draft with a
  table, `finalize_table`; no web call, no text change. The one exception is the **verify-revise loop**
  (`verify/autorevise.py`, `run_verify.py --auto-revise` or `auto_revise.enabled` in
  `verify/config.yaml`, on in the shipped config; `--no-auto-revise` skips a run): after
  the claim pass, a draft with a
  contradicted or unverified claim is revised through the queue's own path
  (`drafter.revise_item` with `claim_problems` and no instructions, `store.revise` with
  note `autorevise.AUTO_NOTE`, `carry_over_checks`), its new claims are checked, and the
  round repeats until every claim is supported or `max_rounds` (per run) /
  `max_rounds_per_draft` (per draft, counted from `revise` decisions with that note;
  0, the shipped value, means no lifetime cap) is
  hit. A revision whose claim set and table rows are unchanged is discarded; a draft with
  an unchecked claim is never revised. A table's contradicted cells join the round as
  `cell_problems` (`autorevise.cell_problems`, claims worded by `tables.cell_claim`, a
  "TABLE CELL FAILURES" section in `build_revision_user_prompt`), so `run_verify.py` runs
  the table pass before the loop and `verify_table` again after each round; blanked cells
  never do. A contradicted cell no longer drops a table: `tables.decide` returns `BLOCKED`,
  `finalize_table` keeps the table and its verdicts without a picture, and the queue's
  approve route drops it then with the count in the note. `claim_problems` and
  `cell_problems` live there and the queue app imports them. The queue blocks approve (409) on a contradicted claim
  unless `override=1`.
- Step 3 reads step 2's tables only through `publish/store.py:fetch_approved`
  (edited_text from `decisions` wins over `thread_json`; it also resolves the draft's
  image path and alt text). Its own tables are
  `schedule` (claim row, one per draft) and `posts` (one row per tweet). Its
  settings live in `publish/config.yaml`, not the root config. Posting is
  idempotent via the claim; partial threads are never retried automatically.
  The queue touches those two tables only through
  `approval_queue/store.py:publish_states` (read-only, empty when the tables are
  missing) to label and hide posted drafts on the approved page, and, on a reopen
  (above), `publish/store.py:forget` and `is_live`, which read and delete step 3's rows
  through step 3's own module.
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
  runs page's Stop button and `run_desktop.py` closing both call it. Run buttons sit on the
  pages they affect (feed: ingest + score; pending: draft, verify; each posts `step` and
  `back` to `/runs`, and the log stays on the runs page; `approval_queue/templates/_run.html`
  renders them from the `current_run` / `publish_live` template globals the panel installs
  on both template envs, so the standalone queue shows none). The one argv the panel builds
  itself is `JobManager.start_publish_now(draft_id)` (`POST /publishing/now` from the
  approved page): `run_publish.py --live --now --draft ID` as its own run, still gated by
  `PUBLISH_ENABLED=1` inside run_publish.py. The other is **automatic publishing**
  (`panel/autopublish.py:AutoPublisher`, started and stopped by the app's lifespan so a
  test client never runs it): while `auto_publish_enabled` in `publish/config.yaml` is on
  and `PUBLISH_ENABLED=1`, `tick(now)` (pure decision, clock as a parameter) starts
  `JobManager.start_publish_auto()`, `run_publish.py --live` with no other flag, every
  `auto_publish_interval_minutes`, retrying on the next 30 s poll when a run is in
  progress. Those two are the only argvs that carry the flag (`_launch_publish`), and
  `FORBIDDEN_ARGS` still refuses it in any configured step. An automatic run whose log
  says "nothing to post" is `Job.quiet`: kept out of the runs page and `pipeline_runs`.
  `POST /publishing/auto` writes the switch and interval through
  `publish/scheduler.py:save_auto_publish` (same line edit as `save_caps`).
  "Set schedule" on the approved page (`POST /publishing/order`, `panel/publishing.py`)
  writes the human's order to step 3's `schedule.position` (guarded migration in
  `publish/store.py`, `set_order`, unclaimed rows only); `scheduler.rank` puts ordered drafts
  first, then breaking, then policy; `store.publish_states` reads it back for the pill. The
  panel never writes `config.yaml`, `draft/voice.md` or a draft's text. The one settings
  file it edits itself is `publish/config.yaml`, four keys only (the included queue routes
  add `trusted_domains` in `verify/config.yaml`, above): `POST /publishing/caps` calls
  `publish/scheduler.py:save_caps` (`max_posts_per_day`, `min_gap_minutes`; line edits,
  comments kept) and `POST /publishing/auto` calls `save_auto_publish` (the two
  `auto_publish_*` keys). It has
  no authentication: `run_app.py` binds localhost by default. `/publishing` and
  `/feedback` are otherwise views: no post button, and a report's suggestions are rendered,
  never applied. The desktop build (`run_desktop.py`, `pipeline_cli.py`, `deploy/desktop.spec`)
  changes no step: `panel/frozen.py` decides the data dir (exe folder when frozen, else
  the repo root), the step interpreter (`pipeline-cli` when frozen) and the bundle
  manifest (every `*/config.yaml`, `draft/voice.md`, both template dirs, each CLI script
  as a marker for `ops/runner.py:cli_missing`, which also looks in `sys._MEIPASS`).
  `pipeline_cli.py` dispatches only the names in `panel/frozen.py:CLIS`. pywebview and
  PyInstaller live in the `desktop` extra only.
- **Step 9 (`swarm/`) writes a thread one post at a time from many cheap calls.**
  `swarm/genome.py:Genome` (slots + rules + `fan_out`/`layers`, seeded from
  `DEFAULT_GENOME` into `swarm_genomes`) is the heritable part; `swarm/prompts.py`
  and `swarm/cells.py` are pure (a cell sees the brief, its slot's rule, the earlier
  chosen cells and the per-post hard rules, never the whole thread);
  `swarm/engine.py:run_swarm` does proposals, Mixture-of-Agents synthesis layers,
  `cell_problems` drops, `dedupe`, a pairwise-judge `tournament` per slot and one
  assembly through `draft/drafter.py:generate` (the public name of the draft_item
  attempt loop: schema, `check_hard_rules`, `chart_problems`, retries), and
  `compare` is the jury against the control draft (ties go to the control). Every
  call takes `call=` and defaults to `draft.drafter.call_anthropic`; no new network
  module and no `claude-*` ID in code (`swarm/config.yaml` holds the cheap model).
  `run_draft.py:draft_with_swarm` stores the winner through `store.insert_draft`
  exactly as before, so verify, the queue, publish and feedback are unchanged;
  `swarm/store.py` owns `swarm_runs` / `swarm_variants` / `swarm_genomes` /
  `swarm_fitness`; its one read of another step's tables is `fetch_head_metrics`
  (step 3 `posts` + step 4 `tweet_metrics`, read-only, empty when missing).
  `run_draft.py` drafts the live genomes round-robin (`next_genome`; the seeds are
  `swarm/genome.py:SEED_GENOMES`) and picks a designer the same way (`next_designer`,
  `swarm_runs.designer_id`; `images.attach_chart(style=)` is the Style the first render
  starts from). **A genome owns its topology**: `swarm/config.yaml` `fan_out`/`layers`
  are only the fallback for a row without them. `swarm/fitness.py` is pure (relative
  KPI against the trailing median, per-genome scores keyed by `genome_id` or
  `designer_id`, `bet_summary`, `prune`). `swarm/mutate.py` breeds: `breed_writer` is
  the one strong-model call (through `call_anthropic`) and `validate_child` /
  `diff_count` enforce exactly one change inside the bounds in `swarm/genome.py`;
  `breed_designer` is pure code (one Style knob stepped, a flag flipped or the palette
  swapped; `evolve.designer_population_size` is their population). `run_evolve.py` writes only
  `swarm_fitness` and `swarm_genomes` and is the `evolve` step in `ops/config.yaml`.
  `swarm_genomes.kind`, `swarm_runs.designer_id` and `swarm_fitness.designer_id` are
  guarded migrations. The panel's `/swarm` page reads through
  `ops/store.py:fetch_swarm_population` / `fetch_swarm_bet` (read-only, empty when
  missing) and renders `panel/views.py:swarm_rows` / `bet_summary_row` (pure); it never
  breeds or retires. **Phase four**: `swarm/genome.py:FormatGenome` (kind `format`,
  `SEED_FORMATS`: thread with one picture, with two, with none, single post, long post) is
  drafted round-robin (`next_format`, `swarm_runs.format_id`, `swarm_fitness.format_id`,
  guarded); `run_draft.py` turns it into a `Format` with `formats.long_max_chars` and
  hands the same one to the swarm and the control. The engine runs ONE cell
  (`prompts.SINGLE_SLOT`) for a single post and the genome's slots as sections of
  `formats.long_section_chars` for a long post (`cells.cell_problems(max_chars=,
  needs_url=, needs_preprint=)`), and `assemble_prompt(..., fmt)` says the shape. Formats
  are pruned with `evolve.format_min_posts` (a coarse gene needs more posts) and bred by
  pure code (`mutate.breed_format`: one field stepped to a neighbour, named by
  `format_name`); `evolve.format_population_size` is their population.
  `tests/conftest.py` turns the swarm off for every test that does not opt in.
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
timeutil.py  display timezone: UTC storage -> one human-facing zone (root `timezone:`),
          fmt_datetime/fmt_date, Jinja |localtime / |localdate
claude_cli.py  optional headless LLM backend (llm_backend, run_claude)
draft/    schema.py (Draft, Format, validate_output), hook.py (rule 12: the opening post),
          chart.py (chart + table specs, verification, PNG rendering, Style
          knobs, 3D header, logos), grader.py (image grader: ImageGrade, CHECKLIST,
          grade_image, call_grader), branding.py (tickers + logos for company cells),
          logos.py (site icon discovery + PNG normalisation for run_logos.py), prompt.py,
          voice.md, drafter.py, config.yaml, settings.py, tags.py (Handle, load_handles,
          relevant_handles, trial_names, drug_names, tag_problems),
          examples.py (EditExample, select_edit_examples, format_examples_block),
          voice_report.py (VoiceReport, build_report, render_markdown, CLI)
approval_queue/  store.py (drafts, decisions, draft_examples, fetch_candidates,
          fetch_decisions_for_voice, fetch_draft_stats, record_examples, image_dir,
          set_image, drop_image, image_grades), images.py (attach_chart, attach_table,
          render-grade loop), app.py (/voice,
          /drafts/{id}/image), templates/
panel/    views.py (pure view models, sparkline geometry), feed.py (scored feed +
          ratings), jobs.py (JobManager, background step runs), autopublish.py
          (AutoPublisher: the timed publish loop), frozen.py (data dir,
          step interpreter and bundle manifest for the desktop build),
          app.py (dashboard, /sources, /feed, /runs, /publishing, /feedback, /swarm), templates/
swarm/    config.yaml, settings.py, genome.py (Slot, Genome, DEFAULT_GENOME), prompts.py
          (Brief, cell/judge/assembly prompts, parse_winner), cells.py (cell_problems,
          dedupe, tournament), engine.py (run_swarm, compare, SwarmFailed),
          fitness.py (score, genome_scores, bet_summary, prune), mutate.py (Parent,
          breed_writer, validate_child, breed_designer, breed_format, format_neighbours),
          store.py (swarm_genomes with kind writer|designer|format, swarm_runs,
          swarm_variants, swarm_fitness, next_genome, next_designer, next_format,
          fetch_head_metrics, fetch_winning_threads)
verify/   config.yaml, settings.py (add_trusted_domain), verifier.py (ClaimCheck,
          verify_claim, call_model), store.py (claim_checks, table_checks,
          mark_host_trusted), tables.py (cell claims, source-backed cells, the render/drop
          decision), render.py (finalize_table: decide, then draw or drop)
publish/  config.yaml, scheduler.py, thread.py, store.py (schedule, posts,
          fetch_approved, is_live, forget = release_unclaimed + release_failed),
          client.py (post_tweet, upload_media,
          verify_credentials)
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
run_logos.py  run_evolve.py   (CLIs)
```
