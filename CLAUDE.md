# CLAUDE.md

Project guidance for Claude Code. Read PLAN.md before making changes.

## What this is
A human-in-the-loop pipeline that ingests immuno-oncology news, scores it with
Claude, and writes X posts (the studio) for human approval. Every model call runs the Claude
Code CLI (`claude -p`) logged in with the operator's account (`claude login`);
there is no Anthropic API path and no API key. The account is the
business and investing side of immuno-oncology biotech (CAR-T and cell therapy,
T-cell engagers and bispecifics, adjacent IO science): trial results and what
they mean, upcoming catalysts for public companies, M&A and financing. The AI
writes as a PhD-level immuno-oncology analyst at a hedge fund. Python 3.11+,
SQLite.
Built steps: 1 ingest + dedup + prefilter + score + digest, 2 the human approval queue,
3 publish to X, 4 feedback loop, 5 operations (orchestrator, health, alerts, backups),
6 conference abstracts + KOL X list + HTTP retry, 8 control panel (one web app over the
whole workflow), 10 the studio (one long Claude Code session per post on Opus 5.5 at max
effort: research, fact base, long post, cards, its own cold fact-check; the app checks it
and queues it; `run_studio.py`, `/studio`). The studio is the only writer: step 2's single
drafter (`run_draft.py`), step 2b's claim checker (`run_verify.py`), step 7's voice
learning loop and step 9's swarm (`swarm/`, `run_evolve.py`, the A/B pick pages, `/swarm`)
were retired and their code removed. Their old tables (`draft_examples`, `image_grades`,
`swarm_*`, `claim_checks`, `table_checks`) are left alone in an existing database, never
read or written. The
kickoff prompt that built
each step is in `prompts/` (see `prompts/README.md`). Nothing posts unless
`PUBLISH_ENABLED=1` **and** `--live`, and posting is **manual only**: a human presses
"Publish now" on the approved page or runs `run_publish.py --live` by hand. Nothing posts on
a timer (the panel has no automatic publisher, its automatic runs start only ingest, score,
the studio's steps and feedback, and `run_ops.py run` refuses any configured step
carrying `--live`).

## Commands
- Install: `pip install -e ".[dev]"`; running the pipeline (not the tests) also needs the
  Claude Code CLI on PATH, logged in (`claude login`)
- Lint: `ruff check .` and `ruff format --check .`
- Test: `pytest`
- Run: `python run_ingest.py`, `python run_score.py`,
  `python digest.py [--rate] [--auto-rate]` (the editor answers yes/no per story plus
  a required explanation that starts with a reason category from
  `score/editorial.py`; stored in `ratings` as 5/1, and the model rater answers the
  same question as `rater='auto:<model>'`; human decisions stay the ground truth),
  `python run_queue.py` (approval UI on localhost:8000),
  `python run_app.py` (control panel: dashboard, sources, runs and the queue, same port;
  the run buttons sit on the pages they affect and "Publish now" lives on the approved page),
  `pythonw run_desktop.py` (the panel in a native window; `pyinstaller deploy/desktop.spec`
  builds `dist/Pipeline/` with `Pipeline.exe` + `pipeline-cli.exe`; both need the
  `desktop` extra), `python pipeline_cli.py <run_x.py> ...` (the CLIs behind one entry point),
  `python run_publish.py` (dry run by default; `--live` needs `PUBLISH_ENABLED=1` and is
  only ever run by a human, never by cron or the ops step; `--draft ID` targets one
  approved draft),
  `python run_feedback.py snapshot|report|followers`,
  `python run_studio.py [--now|--resume-only|--topic T|--story ID] [--angle KEY]
  [--checkpoint|--no-checkpoint] [--list] [--dry-run]` (step 10: the studio's automatic run,
  or one piece now; see `studio/config.yaml`), `python run_studio.py --scan|--scan-now
  [--dry-run]` (step 10's radar: the daily news scan, when due or now),
  `python run_studio.py --learn|--learn-now [--dry-run]` (step 10's learning loop: score
  the posted pieces against X, rewrite the playbook when due or now),
  `python run_ops.py run|health|backup|status|prune` (cron orchestrator; see
  `ops/config.yaml` and `deploy/`)

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
- **Network I/O is confined** to `ingest/http.py` (`get_text`, `get_json`;
  the only caller of `httpx.get` is its private `_request`, which retries
  429/5xx/transport errors and never logs headers),
  `PubMedSource.esearch/efetch` (Entrez), and `Scorer.create_message`
  (Claude), `score/rater.py:call_model`
  (the `digest.py --auto-rate` second-opinion rater, and the call behind
  `filter/link.py` story linking), `studio/scan.py:call_scanner`
  (the radar's daily scan: the CLI with `tools=["WebSearch","WebFetch"]`, its own time
  limit), `studio/playbook.py:call_rewriter` (the studio's learning loop: one playbook
  rewrite, no tools, its own time limit), `claude_cli.run_claude` (the only place that
  spawns the Claude Code CLI and the
  app's only way to reach Claude beside `claude_cli.run_session`, the studio's sessions:
  every Claude call site above routes through it; there is no Anthropic API
  path, no `anthropic` SDK and no API key, and `claude_cli.cli_env` drops
  `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` from the child's environment so the
  CLI always runs on its own login, drops `CLAUDE_AUTO_BACKGROUND_TASKS` and sets
  `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`, so nothing a call starts (a studio
  session's fact-check agent) is moved to the background and killed unfinished when
  the call ends; each one-shot call runs from a new private folder removed afterwards,
  `claude_cli.private_workdir`, never the repo or the shared temp folder, with
  `--safe-mode` from the root `claude_code.safe_mode`, on unless false, so the operator's
  CLAUDE.md files, hooks, MCP servers and output styles never reach a call), and
  `publish/client.py`
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
  (`score/rubric.py:PROMPT_VERSION`), and the raw CLI reply (`raw_response`). Bump
  `PROMPT_VERSION` whenever the prompt, few-shot examples, or tool schema
  change; `run_score.py` then re-scores automatically.
- **Scoring answers a strict JSON schema** (`score/rubric.py:TOOL`). The CLI's print
  mode has no tool calling, so `Scorer.headless_system_prompt` puts the schema in the
  system prompt and `Scorer.create_message` checks the reply in code (not a JSON object
  with a `scores` list: `ScoringError`, the batch is skipped, not retried).
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
  dashboard/publishing/feedback pages, the queue's draft detail page,
  `panel/feed.py`, `digest.py`, `run_ops.py status`, `ops/health.py`'s report heading,
  `ops/alert.py`'s alert body, `feedback/report.py`'s heading. Still UTC on purpose, as sort/parse keys: backup
  filenames `backups/pipeline-<UTC stamp>.sqlite`, the `run_id` stamps and
  `feedback/store.py:day_of`'s `captured_on` bucket; relative ages are zone-independent.
  `publish/config.yaml` and `feedback/config.yaml` keep their own `timezone:` because
  those drive behaviour (posting slots, the "hour posted" column), not display; all
  three are set to the same zone. The behaviour that follows the root `timezone:` is
  `auto_run_times` in `ops/config.yaml` (the panel's automatic runs): the human types and
  reads those times on the same page, so `panel/autorun.py` reads the zone through
  `timeutil.display_tz()` (never config.yaml itself) and `ops/autorun.py` takes it as a
  parameter; a zone change needs a restart, as `timezone_name` is cached. The studio's
  `auto.max_new_per_day` counts by the date in that zone too (`studio/runner.py:
  allowed_to_start(tz=)`), since the automatic runs it rides on are timed in it.
- Read secrets from `.env` via python-dotenv; never commit `.env`.
- Logging: stdlib `logging`. INFO for per-source counts, DEBUG for items.
- Ask before adding a dependency not already in `pyproject.toml`.
- Generated content must never contain medical advice or investment advice
  (no buy/sell/hold/short calls, no price target of the account's own, no return promises;
  describing a thesis, a valuation or a risk is fine). An analyst's published target is
  cited only with what it rests on (`draft/targets.py`, pure, finds the citations: the
  phrase, a per-share figure in dollars or a listing's currency next to target, PT, PO or
  fair value, a fact base's rating shorthand; links are blanked first, and "targets PD-1",
  the median target lesion, a $5B target market, a revenue or EPS target, a takeover
  target's deal price and a CVR's fair value are not): a studio piece
  lists each in piece.json's `price_targets` (`qa.TARGET_FIELDS`: firm, target,
  `previous`, date, `rests_on`, the `catalyst`s the post says to watch, `in_model`
  yes/no/partly/unknown, `effect` unless yes, the firm's published `cases`, `post_says`,
  `source`), held to it by `studio/qa.py:check_price_targets` (fixable: a cited target
  with nothing listed, an entry missing a field, `post_says` in no post or card, a firm
  listed twice, a figure given as a target (a card's table of targets included,
  `qa.card_table_targets`) that no entry lists as its target, previous or
  case), while `studio/safety.py:advice_problems` blocks a target, fair value, value per
  share or computed change of the account's own (`_OWN_TARGET`, per-share figures only)
  in a post, on a card or in its alt text. Preprints are labelled as preprints.
- **Mentions use known handles only.** `draft/tags.py:load_handles` reads the X handles
  `config.yaml` gives (`x:` on a `companies.feeds` / `branding.companies` entry, and the
  `mentions:` list of journals, societies and regulators with `domains:` and
  `match_names:`); `studio/runner.py:app_handles` hands them to the studio's research and
  write prompts, and they are the set `runner.known_handles` lets `studio/qa.py` accept without a
  verifying page. Handles are never guessed. `tags.py` also keeps `tag_problems`,
  `nct_ids`, `drug_names` and `company_names` (a configured company name is never a drug,
  so Genmab is not `#Genmab`), which no pipeline code calls since the drafter was retired.
- **No post carries a link.** Not the source URL, not a registry link, not a bare
  domain: X shows a post with an outbound link to fewer non-followers and posting a URL is
  billed as an extra request through the X API, so the source is named in words (the
  journal, the company, the meeting) with its @handle where one is known.
  `studio/safety.py` blocks a link or a bare domain in a post or on a card. Nothing has
  to carry a URL, so `draft/schema.py:Format` normalises every non-thread shape to exactly
  one post (`SINGLE_SHAPE_POSTS`) and `publish/thread.py:split_thread` neither requires a
  URL nor numbers that one post.
- **KPIs are weighted, not counted.** `feedback/models.py:CONVERSATION_WEIGHTS` defines the
  derived `conversation` KPI (reply/quote x3, bookmark/repost x2, like x1, impression x0.05) beside the six
  stored counts; `Metrics.get` serves it, and it is the shipped `kpi:` in
  `feedback/config.yaml`, so the report points at conversation rather than at reach.
- **A draft's shape and pictures.** `Draft.thread` is always the list of posts;
  `draft/schema.py:Format` (shape `thread` | `single` | `long`, `min_posts`/`max_posts`,
  `visuals` 0-2, one anchor word per visual, `max_chars`) is what a draft carries: a studio
  piece is shape `long` at the studio's `x.long_post_max`. `Draft.shape`, `anchors`
  (1-based post per visual) and `max_chars` are stored in `drafts.format_json` (guarded
  migration) and every picture in `drafts.images_json` (`store.set_image(..., index=k)`,
  `image_file(id, k)` under `store.image_dir()`, `<db folder>/images/`;
  `image_path`/`image_alt` stay the first picture). `draft/chart.py` keeps only the chart
  and table SPEC types (`Chart` with `kind` `bars` | `grouped` | `stat`, `Table`), their
  validation, the JSON round trip (`visual_from_json`) and `alt_text`; it draws nothing
  (the matplotlib renderer, the image grader and the `images` extra were removed). The
  queue serves a picture at `/drafts/{id}/image`, and `store.drop_image` (every picture)
  or `POST /drafts/{id}/image/{index}/drop` (one) is the only way a human removes one.
  Step 3 attaches each picture to the post it is anchored to (`publish/store.py` reads
  `format_json`/`images_json` into `Approved.shape`, `max_chars`, `images`;
  `run_publish.images_for` / `publish_one(images=)`; `publish/thread.py` checks each post
  against the draft's `max_chars`; `publish/client.py:upload_media`, v2 media/upload +
  media/metadata, then POST /2/tweets with `media_ids`); `media.attach_images` in
  `publish/config.yaml` turns that off, an upload failure before post 1 posts nothing and
  marks the draft `failed`, a later one leaves a partial thread.
- Step 2 is the approval queue (`approval_queue/`); the studio is what fills it.
  **One story, one piece of writing**: the studio's shortlist skips a story with a draft
  that did not fail (`store.drafted_cluster_ids`, followed through the draft's item), and
  research fails a piece whose named story got such a draft while it ran.
  Its own tables are `drafts` and `decisions`; edits log original vs edited text
  (`store.parse_decision_text` reads a decision's text back). A leftover `choosing` draft
  from the retired A/B pick is migrated to `pending` by `store.connect`.
  An approve is reversible: `POST /drafts/{id}/reopen` (`store.reopen`, a `reopen`
  decision carrying the text and the optional note) puts an approved draft back to
  `pending`. `POST /drafts/{id}/release` is the other half: a draft whose publish attempt
  posted nothing goes back in line WITHOUT leaving the approved list (no decision row, the
  saved order kept) by dropping step 3's dead schedule row — `failed`/`refused` through
  `publish/store.py:release_failed`, and a `claimed` row left by a run that died through
  `release_claimed`, which refuses a claim younger than `store.STALE_CLAIM_MINUTES` (a run
  may still be posting that thread) and anything with a live `posts` row.
  `publishing.release_reason` is the one gate, read by the route and by the button on both
  templates, as `block_reason` is for reopen. `approval_queue/publishing.py` is the queue's one door to step 3 (as
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
  The queue changes a draft only by hand: approve, edit (`POST /drafts/{id}/edit`, a long
  draft held to its own `max_chars`), reject, reopen, release and the picture drops. A
  change in words goes to the studio: a studio piece is revised from its `/studio` page
  (`store.revise`, a `revise` decision holding the before/after); publish honours the
  latest `edit` or `revise` text.
- Step 3 reads step 2's tables only through `publish/store.py:fetch_approved`
  (edited_text from `decisions` wins over `thread_json`; it also resolves the draft's
  image path and alt text). Its own tables are
  `schedule` (claim row, one per draft) and `posts` (one row per tweet). Its
  settings live in `publish/config.yaml`, not the root config. Posting is
  manual only (see the top of this file) and idempotent via the claim; partial threads
  are never retried automatically. `publish/thread.py` appends " (n/N)" to a thread's
  replies only under the shipped `thread_numbering: replies` in `publish/config.yaml`,
  keeping the opening post marker-free (`all` numbers it too, `none`
  numbers nothing; `scheduler.numbering_mode` normalises the value).
  The queue touches those two tables only through
  `approval_queue/store.py:publish_states` (read-only, empty when the tables are
  missing) to label and hide posted drafts on the approved page, and, on a reopen or a
  release (above), `publish/store.py:forget`, `release_failed`, `release_claimed` and
  `is_live`, which read and delete step 3's rows through step 3's own module.
  Texts are re-checked before posting and refused, never edited, on failure.
- Step 4 reads other steps' tables only through `feedback/store.py:fetch_posted`
  (posts) and `fetch_post_context` (drafts/decisions/items/scores/ratings). Its
  own tables are `tweet_metrics`, `follower_snapshots`, `feedback_reports`; its
  settings live in `feedback/config.yaml`. `feedback/client.py` is the only
  module that calls the X API (httpx, `X_BEARER_TOKEN`, read-only). Reports
  PROPOSE rubric/prefilter/slot changes; a human applies them and bumps
  `PROMPT_VERSION`. Analysis and suggestions are pure (no DB, no network). Studio posts
  are the report group `studio` (`fetch_post_context`); the feed (`sources[...]`) and
  voice-guide proposals compare the retired drafter's posts only (`suggest.NOT_A_FEED`,
  `_drafter_rows`; the voice-guide ones still name `draft/voice.md`, removed with it).
- Step 5 (`ops/`) never imports another step's modules: `run_ops.py run`
  executes the other CLIs as subprocesses (order, timeouts, enabled/required in
  `ops/config.yaml`, which must never contain `--live`; a test asserts it) under
  an `fcntl` lock. A plain `run` leaves out `manual` steps and `skip_when_busy` ones (the
  studio: a session would hold the run and its lock for an hour or more, and Task Scheduler
  and a systemd oneshot never start a run still going), which run with `--only`; the
  studio's own schedule entry is `run --only studio` (`deploy/pipeline-studio.service`,
  `TimeoutStartSec=infinity`), and a run of `skip_when_busy` steps alone takes no run
  lock, only the step's own. `REPO_ROOT` here and in `panel/frozen.py` (and the frozen
  `data_dir`) is `os.path.abspath`, never `resolve()`: on Windows that would turn a mapped
  drive into a UNC path, where the npm `claude.cmd` cannot start a studio session. It reads other steps' tables only through the read-only
  adapters in `ops/store.py` (each returns empty when a table is missing) and
  owns `pipeline_runs`, `health_checks`, `alerts_sent`. `ops/health.py` is pure
  (`now` is a parameter; its `cli` check, `check_cli(binary, found)`, is handed the
  Claude Code CLI's resolved path by `run_ops.cli_status`, a `shutil.which` of the root
  `claude_code.binary`, and `run_all(cli=None)` skips it; `health.required_env` ships
  empty, since Claude needs no key). The only network call in `ops/` is
  `alert.py:post_webhook` (plus `send_email` via smtplib); alerts carry check
  names, summaries and counts, never secrets or post text. `ops/autorun.py` is pure (no
  DB, network or clock): `parse_times` (HH:MM, at most `MAX_TIMES`, `MIN_SPACING_MINUTES`
  apart round the clock, YAML's base-60 ints read back), `slots_between` / `next_slot`
  (wall-clock times in a zone that is a parameter), `settings_of` and `plan` /
  `ineligible`, the allowlist: a step may run automatically only as
  `python <AUTO_SCRIPTS>` (ingest, score, studio, feedback) with nothing
  starting like the live flag (`posts_live`, which `run_ops.py` now uses too, so an
  abbreviated flag is refused; `run_publish.py` parses with `allow_abbrev=False`).
  `ops/config.py:save_auto_run` writes only `auto_run_enabled` / `auto_run_times` (top-level
  line edits, times quoted, refused unless the parsed file is otherwise identical, swapped
  in with `os.replace`). Health's `source_stale_min_hours` is a floor under
  `source_stale_multiplier * cadence`, and the shipped staleness limits (13h) cover the
  longest gap between the shipped `auto_run_times` (a test asserts it).
- Step 8 (`panel/`) owns no tables, no config file of its own and no pipeline
  logic. It reads other steps only through `ops/store.py`'s read-only adapters plus
  `run_ops.build_report`; the one exception is `panel/feed.py`, which uses step 1's own
  `db.Database` API (as `digest.py` does) to list scored clusters and to write a human
  yes/no decision — the only row the panel writes outside its own pages, and it opens that
  Database inside the route because a `Database` keeps its connection to one thread.
  The look of every page (panel, queue and studio) is one stylesheet, the `<style>` block of
  `approval_queue/templates/base.html` (a dark synthwave theme: tokens on `:root`, decoration
  only in pseudo-elements behind the page's one content panel, the functional rules last); a
  page template that styles itself uses those tokens with a fallback
  (`var(--border, #ddd)`, `--surface-2`, `--ok`, `--chart-line`), never a colour of its own.
  It renders through the pure functions in `panel/views.py`
  (`now` is a parameter; no DB, network or clock), and includes the step 2 queue's
  routes into the same app so the queue's own module stays unchanged apart from its
  index moving to `/queue`. `panel/jobs.py` never builds an argv: a job names steps
  from `ops/config.yaml` and `ops/runner.py` runs them, refusing any step whose argv
  contains `--live`. **Runs go side by side, one step never twice**: `run_steps(lock_path=)`
  takes `ops/lock.py:step_lock_path(lock_path, step.lock_name)` around each step (`lock:` in
  `ops/config.yaml`, default the step's name; ingest and score share `stories` because
  story linking deletes clusters ingest may be filling), and a held lock skips the step as
  `runner.SKIP_LOCKED`. The panel keeps any number of live jobs (`JobManager.running()`,
  `busy_steps()`), refuses a start whose lock a live job in the same window holds, and the
  run bar disables only those steps' buttons (`current_runs` / `busy_steps` template
  globals). `run_ops.py run` still takes `lock_path` itself so cron fires do not overlap. `JobManager.cancel()` ends a run:
  `ops/runner.terminate_active()` kills the live step's process tree (own process group on
  POSIX, `taskkill /T` on Windows) and the remaining steps are skipped as `cancelled`; the
  runs page's Stop button and `run_desktop.py` closing both call it. Run buttons sit on the
  pages they affect (feed: ingest + score; the studio pages: the studio steps; each posts `step` and
  `back` to `/runs`, and the log stays on the runs page; `approval_queue/templates/_run.html`
  renders them from the `current_run` / `publish_live` template globals the panel installs
  on both template envs, so the standalone queue shows none). The one argv the panel builds
  itself is `JobManager.start_publish_now(draft_id)` (`POST /publishing/now` from the
  approved page): `run_publish.py --live --now --draft ID` as its own run in a second slot beside the
  pipeline's (so it can start mid-run; one publish at a time, under the publish step's lock `<lock_path>.publish`
  rather than the pipeline lock; each job has its own stop flag, `runner.run_steps(stop=)`,
  so `cancel(job_id=)` stops one run), still gated by
  `PUBLISH_ENABLED=1` inside run_publish.py. It is the only argv that carries the flag
  (`_launch_publish`), and `FORBIDDEN_ARGS` still refuses it in any configured step.
  **Posting is manual only**: there is no automatic publisher (the timed
  `panel/autopublish.py` loop, its `POST /publishing/auto` switch and the
  `auto_publish_*` keys were removed), so nothing posts unless a human pressed the button.
  **Automatic runs are everything but publishing** (`panel/autorun.py:AutoRunner`, started
  and stopped by the app's lifespan; `run_desktop.stop_run` stops it before cancelling runs):
  at each `auto_run_times` it calls `JobManager.start(auto_run_steps, auto=True)`, and that
  automatic mode re-checks `ops/autorun.ineligible` for every step (so no caller can skip
  the allowlist), runs with `jobs.AUTO_ENV` (`PUBLISH_ENABLED=0`, which `.env` cannot
  override) and records under `ops/store.py:AUTO_RUN_MARK` in the run_id, followed by
  `run_ops.health_and_alert(send=True)` as cron's run does; `_launch` refuses `auto` with
  `publish`. `tick(now)` is the whole decision: a watermark `last` that moves on every tick
  whatever happens (so switching on, adding a past time, a leader handover or a clock set
  back never fires), a pending time that waits for its busy steps (`busy_steps`, which
  counts only the unfinished steps of a live job, `Job.lock_names`) up to
  `auto_run_grace_minutes`, several missed times run once, and one leader per data dir
  (`<lock_path>.autorun`, `lock.acquire(trust_os_lock=True)`, path resolved against
  `data_dir()`). **Daily backup**: when a run starts and `ops/autorun.py:backup_due` finds the
  newest backup older than `auto_run_backup_hours` (20 shipped, 0 off), `tick` takes one after
  releasing its mutex through the `backup` callable (`panel/app.py:_backup_now`, the same
  `ops/backup.py:backup` as "Back up now"), noting "backup saved/failed" in the outcomes;
  `ops/backup.py` writes `<name>.part` and renames it after `integrity_check`, so a backup cut
  short never counts as the newest; it also copies each file named in `backups.with_db`
  (shipped: `studio_playbook.md`) from the database's folder beside the backup as
  `pipeline-<stamp>.<name>`, and `rotate` removes those with their backup. The runs page shows it and posts `POST /runs/auto` (switch and times only);
  the dashboard shows the state and `ops/store.py:last_auto_run`. `tests/conftest.py`
  disables `AutoRunner.start` for every test; `tests/test_autorun.py` drives `tick`.
  "Set schedule" on the approved page (`POST /publishing/order`, `panel/publishing.py`)
  writes the human's order to step 3's `schedule.position` (guarded migration in
  `publish/store.py`, `set_order`, unclaimed rows only); `scheduler.rank` puts ordered drafts
  first, then breaking, then policy; `store.publish_states` reads it back for the pill.
  The studio performance page's "add the post's link" (`panel/publishing.py:add_head_link`,
  wired into `studio/web.py` as its `add_link` hook) writes post 1's X id through step 3's
  own `publish/store.py:set_head_tweet`, only over a `manual-` marker. The
  panel never writes `config.yaml` or a draft's text. The settings it
  edits itself are two keys of `publish/config.yaml` and two of `ops/config.yaml`
  (`auto_run_enabled`, `auto_run_times` through `ops/config.py:save_auto_run`, from
  `POST /runs/auto`; the step list stays file-only): `POST /publishing/caps` calls
  `publish/scheduler.py:save_caps` (`max_posts_per_day`, `min_gap_minutes`; line edits,
  comments kept). The dashboard's "Back up now" (`POST /backup`) calls `ops/backup.py:backup` into `backups.dir` with `backups.keep`, as `run_ops.py backup` does. It has no authentication: `run_app.py` binds localhost by default, and every POST must come from the app's own pages: `approval_queue/app.py:SameOriginOnly` (installed on the queue app and the panel's, so the queue's and the studio's routes too) answers 403 when `Origin`, or `Referer` without one, names another host than `Host` (or is `null`); a request with neither (tests, curl) passes. `/publishing` and
  `/feedback` are otherwise views: no post button, and a report's suggestions are rendered,
  never applied. The desktop build (`run_desktop.py`, `pipeline_cli.py`, `deploy/desktop.spec`)
  changes no step: `panel/frozen.py` decides the data dir (exe folder when frozen, else
  the repo root), the step interpreter (`pipeline-cli` when frozen) and the bundle
  manifest (every `*/config.yaml`, the studio's brief, angles, playbook seed, fonts and
  reference pieces, the template dirs, each CLI script
  as a marker for `ops/runner.py:cli_missing`, which also looks in `sys._MEIPASS`).
  `pipeline_cli.py` dispatches only the names in `panel/frozen.py:CLIS`. pywebview and
  PyInstaller live in the `desktop` extra only.
- **Step 10 (`studio/`) is one long Claude Code session per post.** The app chooses the
  topic and the angles on offer, runs the session, checks what comes back and queues it; it
  never writes or rewrites the post. Every stage is `claude_cli.run_session` (the second
  place that spawns the CLI, beside `run_claude`: stream-json into
  `<piece>/session.ndjson`, a fixed `--session-id` on the first run and `--resume` after,
  so the session keeps what it read; a `--resume` the CLI answers "No conversation found"
  (`claude_cli.session_lost`: Claude Code cleans old sessions up) goes to
  `session.fresh_session`, a new id the piece keeps (`lost_sessions` in its meta), the
  standing instructions again and `prompt.fresh_session_prompt`, which has it read the
  piece's files first; `cwd` is the piece folder; `--append-system-prompt-file`
  with `studio/brief/session.md` + `voice.md` + `cards.md` on every launch, resumes
  included (the CLI reuses its record of the first launch's prompt only until the
  conversation is compacted); a run whose CLI stopped a sub-agent unfinished
  (`task_updated` killed, or `subagent_stats.killed` in the result line) reports
  success all the same, so it comes back `claude_cli.SUBAGENT_KILLED`, not ok, and the
  stage is resumable;
  the reference pieces as the piece's own copy, `<piece>/reference/`
  (`session.copy_reference`, made when a stage starts and none is there; never an
  `--add-dir`, which --restricted would make writable, so no session can change
  `studio/exemplars`); `stage_effort.<stage>` over the piece's effort
  (`settings.stage_effort`; the model never changes within a session); `tools` and the isolation `cli_flags` (`--safe-mode
  --restricted --permission-mode dontAsk`) from `studio/config.yaml`; API keys stripped by
  `cli_env`). Stages (`studio/session.py`): research (`factbase.md`, `research.json`; its
  `story_id`, an int or digit string, sets the piece's cluster only when it is in
  `offered_stories`, the shortlist ids every research run of the piece was offered,
  recorded before the session starts, and has no draft
  that did not fail), an
  optional checkpoint (`research_ready`, the editor's Continue), write (`posts/NN.txt`,
  `cards/card_N.html`, a cold fact-check by a fresh sub-agent logged in `factcheck.md`,
  in the foreground since `cli_env` disables background tasks and told by the prompt
  that pages are data and that it changes no file (`prompt.CHECKER_RULES`: a sub-agent
  gets none of the standing instructions), `piece.json`; a piece
  without that log blocks, `qa.NO_FACTCHECK`), polish rounds (`studio/qa.py`: `piece.json` shape, X-weighted length from
  `studio/xcount.py` with `x.headroom`, `studio/safety.py` blocking lines (investment or
  medical advice and a price target of the account's own, on the cards and their alt text
  too; links incl. bare domains), an @handle without a verifying page, an analyst target
  cited with no entry or with a `post_says` the posts do not contain
  (`check_price_targets`, the voice guide's "Analyst price targets"), cards
  drawn by `studio/render.py`; blocking problems keep a piece out of the queue, fixable ones
  go back to the session up to `max_polish_rounds` and then ride along as warnings, and one
  review round always shows the session its PNGs), then `studio/ingest.py`: a pending draft
  with `item_id` `studio:<piece id>` (`approval_queue/store.py:studio_item_id`,
  `DraftRow.studio_piece`), shape `long` at `x.long_post_max`, no claims, every card copied to `image_file(id, k)` and anchored to its post, each
  `recheck_before_posting` fact a `store.RECHECK_PREFIX` line of `why_it_matters`
  (`store.recheck_lines`, listed by the panel's copy-paste page through
  `Approved.why_it_matters`, plus one line naming the analyst targets the posts still
  cite, `qa.cited_targets` and `ingest._target_line`, or the words citing one no entry
  lists); a revision (`store.revise`) replaces text and cards of a
  pending draft, or of a rejected one that `store.reopen` brings back (refused when
  `approval_queue/publishing.py:is_live` or `block_reason` says step 3 holds it, followed
  by `publishing.forget`, as the queue's Reopen does). Every ingest records what it put in
  (`queued` in the piece's meta); `ingest.hand_edits` compares the draft with it (the
  editor's text, cards dropped), `session.revise` writes those changes into the piece's
  files once (`write_back`, recorded as `hand_edit`) and names them in `revise_prompt`,
  a Resume with unsynced changes goes through a revision, and `to_queue` refuses to
  replace a draft whose changes the session never saw. The studio page shows the queue's
  text when it differs from the files. The queue holds a studio draft while its piece is
  in a running stage or has a request waiting (`store.studio_hold`, read-only on
  `studio_pieces`): approve, edit, reject and the picture drops answer 409, and the list
  and detail pages say "on hold". The queue's revise route
  refuses a studio draft and points at `/studio/<id>`; its edit route takes a long draft's
  own `max_chars`. `studio/render.py` is the only place that launches a browser (Edge,
  Chrome or Chromium, `render.browser` / `STUDIO_BROWSER`), always headless with the
  network blocked and the fonts in `studio/fonts/` injected; it is not network I/O. Every
  card opens with a content policy (`CONTENT_POLICY`: inline styles, data: images and the
  house fonts only; the nonce'd checker is the one script that runs), so a card can never
  draw a local file into its picture, and a meta refresh is refused before launch. The
  checker also reports the page's real viewport: new headless Chromium keeps 87 px of its
  window, so the window is grown by the measured difference (`_WINDOW_EXTRA`, per browser
  per run) and the screenshot cut back to the card by `crop_png` (standard library only).
  An empty band taller than `EMPTY_BAND_SHARE` of the card (text, pictures, chart marks
  and painted leaf boxes projected on the vertical axis; a box holding other elements is
  not content) is a fixable layout problem.
  `studio/store.py` owns `studio_pieces` (stage, session id, workspace, angle, shape,
  hook, draft id, the editor's pending `request`), `studio_runs` (one per CLI run, its
  `cost_usd` that run's own: the CLI reports the session's running total, so
  `session._run_cost` subtracts the total the piece's session last reported) and
  `studio_topics` (queued by the editor); its one read of step 1 is `studio/topics.py`
  through `db.Database`. Each piece and queued topic keeps one item of its story
  (`story_item`, guarded migration; recorded by the queue route, `runner.new_piece` and
  research's `story_id`), and every run first calls `runner.follow_merges`
  (`topics.merged`, `store.repoint_story`), so a story linking folded into another cluster
  is written and excluded there; a story-only queued topic whose story is gone is dropped
  and the next queued topic taken (`runner.take_queued`). Variety is code, judgement is the
  session's: `studio/angles.py`
  offers every angle in `studio/angles.yaml` except the last `avoid_recent_angles` used (a
  human-named angle is the only one offered) and lists recent hooks, shapes and openings to
  avoid (from the last `recent_pieces_shown` written pieces, `Brief.recent`); RECENT
  PIECES in the research prompt is `Brief.topics_to_avoid`, every piece started in the
  last `topics.avoid_days` days that was not discarded, finished or not
  (`store.started_within`, an unwritten one with its status). Research and write both
  carry the playbook, the handles `config.yaml` gives (`runner.app_handles`, the set
  `known_handles` lets qa accept) and, when there are some, a pointer to
  `prompt.EARLIER_FILE`, which `session.write_earlier` puts in the piece folder before
  each of them: the `Brief.recent` pieces' text as the queue holds it and whether it went
  out on X (`ingest.queued_text`), since `--restricted` keeps a session out of other
  pieces' folders and the scorecard angle grades against the account's own bar. Feed
  stories are quoted between `prompt.FEED_TEXT_START` / `FEED_TEXT_END` as data.
  **Voices are the app's to give, never the session's to choose**: `studio/voices.py` is
  pure (the playbook's `## Voices` section, `### key: Name` then how the voice sounds;
  `parse`, `problems`, `without` (what a session reads as the playbook: it is told its own
  voice, never the others), `with_seed` (a copy with no Voices heading reads the seed's;
  a heading with no voice turns voices off), `given`, `changed`). `runner.assign_voice`
  (from `brief_for`, so under the claim lock in research) gives a piece without one a
  voice through `evidence.voice_for` -> `learn.draw_voice` (at random, never
  `store.last_voice`, the newest other piece's, until `voices.lean_min_measured` scored
  pieces carry one, then a Thompson draw; seeded by the piece id) and records it
  (`studio_pieces.voice`, guarded migration, and its words in the meta's `voice`), so every
  stage writes in the voice as worded then: research gets `prompt._voice_ahead`, write
  the YOUR VOICE block, `revise_prompt(voice=)` a reminder. Every voice speaks in the first
  person (voice.md's "Sound like a person"), held to it by `qa.check_first_person`
  (fixable: fewer sentences that `speaks_as_writer`, I/me/my and never a Roman numeral, than
  one per `voices.first_person_every_chars`, capped at `first_person_max`). `voices.enabled:
  false` gives no voice. `studio/runner.py` (run by `run_studio.py`) marks pieces left mid-stage as
  `interrupted`, acts on `request`s (continue, revise), then starts at most one piece:
  explicit `--topic`/`--story`, else the oldest queued topic, else with `--now` an
  automatic topic, else only when `auto.max_new_per_day` (automatic pieces per calendar
  day in the root `timezone:`), `auto.min_hours_between` (any piece), no checkpoint
  wait and fewer than `auto.max_waiting` studio drafts pending in the queue
  (`store.waiting_in_queue`, read-only) allow, and a card browser was found (`make_renderer`; without one the automatic run
  exits 1 rather than spend research and writing on a piece polish would stop); up to
  `max_parallel` runs at once (studio/config.yaml, 3), each holding a writing slot
  (`runner.slot_lock_path`: `<workspace_dir>/.studio.lock`, `.studio.lock.2`, ...; a run
  waits `LOCK_WAIT_SECONDS` for a free one) for its life and recording it in the meta of
  each piece it takes up (`slot`, `runner.piece_slot`); requests, queued topics, the new
  piece and a researching piece's shortlist and `offered_stories` are claimed under
  `.studio.claim.lock` (`runner.claiming`, `Context.claiming`), held for moments, and a
  shortlist leaves out stories offered to a piece researching beside it
  (`store.offered_elsewhere`). The studio steps in `ops/config.yaml` carry `slots: 3`
  (`Step.slots`: a run takes the first free of `<lock>`, `<lock>-2`, ...; the panel's
  `JobManager` counts a lock busy only when every slot is held). A
  killed run (Stop, a reboot, a crash) cannot mark its piece: `runner.settle_stopped`
  does, under the claim lock and only for a piece whose slot no run holds, when a studio page shows or
  acts on a piece in a running stage (`web._current_piece`, the index) and when the panel
  sees a run of `run_studio.py` end (`JobManager.on_finish` = `panel/app.py:_after_run`).
  Every brief reads the date and the playbook when its stage starts. A piece's folder is
  `runner.absolute` (the data folder as given, never resolved: a mapped drive stays one).
  `render.render_card` turns a file it cannot write (a PNG an image viewer holds open)
  into a `RenderError`, and `session._echo` / `run_studio.safe_console` keep a character a
  cp1252 console cannot show from costing a line. A piece folder that
  is gone fails the piece (`session.folder_missing`; `_run_stage` turns the OSError into a
  failed result, so the run goes on), and a Resume of a polish whose draft the queue would
  refuse (`ctx.revisable`) runs no round. `ops/config.yaml` has the automatic `studio` step
  (after `score`, `skip_when_busy`: `panel/autorun.py` leaves a busy one out of a slot
  rather than waiting, and also the steps an earlier run still has queued behind it,
  `JobManager.queued_behind`, so a long session never makes a run time skip ingest and
  score; a plain `run_ops.py run` leaves it out, see step 5) and the manual `studio_now` / `studio_resume` steps, all under the
  `studio` lock (the radar's and the learning steps have their own); `run_studio.py` is in `ops/autorun.AUTO_SCRIPTS`. The panel includes
  `studio/web.py`'s router (`/studio`, a piece's page, its cards, `/studio/playbook`); its
  buttons write studio rows and start those steps through `panel/app.py:_start_studio`,
  and the playbook editor writes only `studio_playbook.md` next to the database (the
  shipped seed is `studio/playbook.md`). `tests/conftest.py` never lets a test spawn the
  real CLI.
  **The radar** is where topics come from. `studio/radar.py` is pure (`today` is a
  parameter): `parse_when` (a day, a month, a quarter, a half, early/mid/late, a year ->
  `When(start, end, text)`, never guessed), `Catalyst.key` (ticker or normalised company,
  kind, drug, start), `parse_scan` (raises `ScanRejected`; caps, http(s) sources, an
  unknown angle blank, catalysts between `past_days` back and `calendar_days` ahead),
  `scan_prompt`, `topic_text` / `catalyst_text` (what a queued radar topic or catalyst
  asks for, with `PREVIEW_ANGLES` / `REACTION_ANGLES`), `coming_up`.
  `studio/scan.py` does the I/O: `run_scan` (one row in `studio_scans` whatever happens;
  a failed call or a rejected answer stores nothing else), `harvest` (a piece's
  `research.json`: its `radar_topic` becomes used, its `catalysts` go on the calendar;
  `session.research` calls it through `Context.harvest` and never fails the piece on it),
  `scan_due` (`radar.scan_every_hours`). `runner.scan` (`--scan`, `--scan-now`, `--scan
  --dry-run`; `<workspace_dir>/.studio_scan.lock`; marks a scan a dead run left running
  as failed) and `runner.radar_for_brief` (an open piece's research brief: the untaken
  topics of the last `topic_days`, `coming_up` catalysts; `Brief.radar`,
  `Brief.coming_up`). `studio/store.py` owns `studio_scans`, `studio_radar_topics`
  (new/queued/used/dismissed, `topic_id` of the queued topic, `piece_id`) and
  `studio_catalysts` (unique `key`, open/dismissed, `origin` scan:<id> or piece:<id>);
  `claim_topic` marks the radar topic or catalyst a queued topic came from with the piece,
  `drop_topic` puts it back. `/studio/radar` (studio/web.py) queues them through
  `queue_topic` and starts `studio_now`. **Repeats are flagged, never blocked**: `studio/repeats.py` is pure
  (`marks`: source URLs, drug names via `draft/tags.py`, development codes, trial names, NCT
  numbers, companies and tickers, title words; `reasons`: a shared source, drug or trial, or
  a company plus `MIN_SHARED_WORDS` title words), compared against `store.covered_since`
  (pieces of the last `radar.repeat_days` days not discarded, and unclaimed queued topics,
  each with the radar topic's or catalyst's companies, sources and drug it came from) by
  `runner.covered_for_repeats`; the radar page shows a "may repeat" flag on an untaken topic
  or catalyst (its button "Write it anyway") and an automatic piece's brief carries
  `Brief.radar_repeats` as a MAY REPEAT line under that radar topic. `ops/config.yaml` has the automatic
  `studio_scan` step (right before `studio`) and the manual `studio_scan_now`, both under
  the `studio_scan` lock.
  **The studio learns from X** (`studio/config.yaml` `learn:`). `studio/learn.py` is pure
  (no DB, network or clock): `pick_snapshot` (a typed-in snapshot as it is, else the
  first at `horizon_hours`), `score` (relative = (value + smoothing) / (median of the
  account's heads in the `baseline_days` before + smoothing), unscored under
  `min_baseline_posts`), `arm_stats` per angle/shape/hook_style/cards bucket/voice, `lean`
  (one Thompson draw per arm, normal posterior on the log scale, prior at the median; an
  angle the editor named is left out; None under `lean_min_measured`; the voice is not in
  it, the app assigns the voice), `draw_voice`, `lean_shares` (the dashboard's share of
  draws), `evidence_block` (WHAT X SAYS, with a small-sample caveat; `voices=False` for a
  session's brief, so a writer never drifts towards the voice that does well),
  `edit_share` / `voice_edits` (per voice, what the editor did with its decided pieces:
  changed by hand and how much, revisions asked, rejected), `rewrite_due` (new = scored
  pieces the last rewrite was not given, `unseen`), and `rewrite_prompt`/`parse_rewrite`
  (JSON, `REQUIRED_SECTIONS` with `## Voices`, `max_words` without it, 2 to 6 well-formed
  voices). `studio/evidence.py:reviews` reads the editor's decisions through the read-only
  `store.fetch_studio_reviews` (drafts, decisions, posts).
  `studio/evidence.py:measure` is the reads: step 3's posted heads and step 4's snapshots
  through `studio/store.py:fetch_posted_heads` (read-only, empty when missing), the
  editor's numbers from `studio_manual_metrics`, a piece found by its draft's `studio:`
  item_id, its cards by `ingest.draft_cards`. `runner.build_brief(evidence=)` puts the
  block and the lean (drawn only among what the variety rules leave: offered angles,
  hooks not used lately, not a shape the last three pieces all used; seeded by the piece
  id) into `Brief.evidence`/`Brief.lean`, which `prompt.py` renders as a WHAT X SAYS
  section in the research and write prompts (none before anything is scored);
  `make_context` measures once per run (`measure_quietly`: a failure never holds a brief
  back) and records each piece's lean in its meta. `runner.learn` (`--learn`, the
  automatic `studio_learn` step after `feedback`; `--learn-now`, the manual
  `studio_learn_now`; both under the `studio_learn` lock and `.studio_learn.lock`, never
  the studio's) rewrites when due through `studio/playbook.py:rewrite` (`learn.playbook`:
  `propose`, shipped, waits for the editor to apply it, `auto` applies unless the rewrite
  changes the voices (`voices.changed`), which always wait for the editor, `off` never; a
  failed call or a rejected reply changes nothing and exits 1). `playbook.current_text` is
  the playbook with the seed's voices when it has none; the editor's save is refused when
  `voices.problems` finds the section malformed. `studio/playbook.py:save` is the one writer of the
  playbook file (atomic) and of `studio_playbook_versions` (`seed`/`editor`/`learned`/
  `proposal`/`revert`, changelog, evidence, `pieces` learned from; `ensure_seeded`
  records the playbook in use before the first change, `revert`, `apply_proposal`).
  `/studio/performance` (`studio/dashboard.py`, pure views) shows "Where the usage
  goes" (`dashboard.usage` over `store.runs_since`: each run's `cost_usd` by stage and by
  where its piece ended up, the last `USAGE_DAYS`), the posted pieces, the
  per-arm table (a voice's share is how often it is drawn), what the editor did with each
  voice (`voice_edit_rows`), the learning state and the version history, takes typed-in numbers and
  the link of a post confirmed by hand without one: `studio/web.py` calls the `add_link`
  hook the panel wires to `panel/publishing.py:add_head_link` ->
  `publish/store.py:set_head_tweet`, which replaces only post 1's `manual-` marker. The
  playbook editor and its reset save versions through `playbook.save`.
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
claude_cli.py  the only way to Claude: the Claude Code CLI in print mode (run_claude,
          build_argv, parse_envelope, cli_env)
draft/    what the studio and the queue share, no model calls: schema.py (Draft, Format,
          validate_output), chart.py (chart + table specs, validation, JSON round trip,
          alt_text; no renderer), tags.py (Handle, load_handles, relevant_handles,
          nct_ids, drug_names, tag_problems), targets.py (price-target citations:
          target_mentions, target_figures, target_problems, field_figure, SHARE_FIGURE)
approval_queue/  store.py (drafts, decisions, image_dir, set_image, drop_image,
          studio_hold, drafted_cluster_ids, parse_decision_text),
          publishing.py (the queue's door to step 3), app.py (/queue, /drafts/{id},
          /drafts/{id}/image), templates/
panel/    views.py (pure view models, sparkline geometry), feed.py (scored feed +
          ratings), jobs.py (JobManager, background step runs, "Publish now", automatic mode),
          autorun.py (AutoRunner: the timer for everything but publishing), frozen.py (data dir,
          step interpreter and bundle manifest for the desktop build),
          app.py (dashboard, /sources, /feed, /runs, /publishing, /feedback), templates/
publish/  config.yaml, scheduler.py, thread.py, store.py (schedule, posts,
          fetch_approved, is_live, forget = release_unclaimed + release_failed,
          release_claimed for a stale claim),
          client.py (post_tweet, upload_media,
          verify_credentials)
feedback/ config.yaml, models.py, analysis.py, suggest.py, report.py,
          store.py (tweet_metrics, follower_snapshots, feedback_reports,
          fetch_posted, fetch_post_context, due_for_snapshot), client.py
ops/      config.yaml, models.py, lock.py, runner.py, health.py, alert.py,
          autorun.py (pure: run times, slot clock, the automatic-run allowlist),
          backup.py, store.py (pipeline_runs, health_checks, alerts_sent +
          read-only adapters)
deploy/   crontab.example, pipeline.service, pipeline.timer, pipeline-studio.service,
          pipeline-studio.timer, desktop.spec, README.md
studio/   config.yaml, settings.py, angles.yaml + angles.py (the angle library, variety),
          brief/ (session.md, voice.md, cards.md: the appended system prompt), playbook.md
          (seed), exemplars/ (reference pieces), fonts/, prompt.py (pure stage prompts),
          session.py (stages), runner.py (one run: stale pieces, requests, new piece),
          qa.py + xcount.py + safety.py (the checks), render.py (cards via headless browser),
          ingest.py (into the queue), topics.py (feed stories), store.py (studio_pieces,
          studio_runs, studio_topics, studio_scans, studio_radar_topics,
          studio_catalysts, studio_playbook_versions, studio_manual_metrics, the
          read-only adapters), radar.py (pure: the scan's prompt and answer, the
          calendar's dates), scan.py (the daily scan, the research harvest), learn.py
          (pure: scores, arms, the lean, the evidence text, the rewrite's prompt and
          checks, the voice draw, the editor's edits by voice), voices.py (pure: the
          playbook's Voices section), repeats.py (pure: does a radar topic or catalyst
          repeat a piece),
          evidence.py (what X says, from the DB), playbook.py (the file, its
          versions, the rewrite), dashboard.py (pure views of /studio/performance),
          web.py + templates/ (/studio pages, /studio/radar)
run_ingest.py  run_score.py  digest.py  run_queue.py
run_app.py  run_desktop.py  pipeline_cli.py  run_publish.py  run_feedback.py  run_ops.py
run_studio.py   (CLIs)
```
