# CLAUDE.md

Project guidance for Claude Code. Read PLAN.md before making changes.

## What this is
A human-in-the-loop pipeline that ingests immuno-oncology news, scores it with
Claude, and drafts X posts for human approval. Every model call runs the Claude
Code CLI (`claude -p`) logged in with the operator's account (`claude login`);
there is no Anthropic API path and no API key. The account is the
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
many pictures on which posts, is a third bred population), 10 the studio (one long Claude
Code session per post on Opus 5.5 at max effort: research, fact base, long post, cards,
its own cold fact-check; the app checks it and queues it; `run_studio.py`, `/studio`). The
kickoff prompt that built
each step is in `prompts/` (see `prompts/README.md`). Nothing posts unless
`PUBLISH_ENABLED=1` **and** `--live`, and posting is **manual only**: a human presses
"Publish now" on the approved page or runs `run_publish.py --live` by hand. Nothing posts on
a timer (the panel has no automatic publisher, its automatic runs start only ingest, score,
draft, verify, feedback and evolve, and `run_ops.py run` refuses any configured step
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
  `python run_draft.py` (`--no-swarm` for the single drafter only), `python run_verify.py`
  (claim checks with web search),
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
  `python run_evolve.py [score|prune|breed|report] [--dry-run] [--force]` (step 9: swarm
  fitness from X and pruning, no network; `breed` makes the one strong-model call per
  writer child),
  `python run_scrub_notes.py [--status STATUS] [--dry-run] [-v]` (operator command: blanks
  picture captions written to the operator in queued drafts and redraws them),
  `python run_unlink.py [--status STATUS] [--dry-run] [-v]` (operator command: strips the
  source URL out of a queued draft's posts, since no post carries a link),
  `python run_studio.py [--now|--resume-only|--topic T|--story ID] [--angle KEY]
  [--checkpoint|--no-checkpoint] [--list] [--dry-run]` (step 10: the studio's automatic run,
  or one piece now; see `studio/config.yaml`), `python run_studio.py --scan|--scan-now
  [--dry-run]` (step 10's radar: the daily news scan, when due or now),
  `python run_studio.py --learn|--learn-now [--dry-run]` (step 10's learning loop: score
  the posted pieces against X, rewrite the playbook when due or now),
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
  (Claude), `draft/drafter.py:call_anthropic` (an older name: it runs the CLI),
  `score/rater.py:call_model`
  (the `digest.py --auto-rate` second-opinion rater, and the call behind
  `filter/link.py` story linking), `draft/grader.py:call_grader` (the image
  grader: the CLI with `tools=["Read"]` opens the PNG), `studio/scan.py:call_scanner`
  (the radar's daily scan: the CLI with `tools=["WebSearch","WebFetch"]`, its own time
  limit), `studio/playbook.py:call_rewriter` (the studio's learning loop: one playbook
  rewrite, no tools, its own time limit), `claude_cli.run_claude` (the only place that
  spawns the Claude Code CLI and the
  app's only way to reach Claude: every Claude call site above, and step 2b's
  `verify/verifier.py:call_model`, routes through it; there is no Anthropic API
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
  dashboard/publishing/feedback pages, the queue's draft detail and voice pages,
  `panel/feed.py`, `digest.py`, `run_ops.py status`, `ops/health.py`'s report heading,
  `ops/alert.py`'s alert body, `feedback/report.py`'s heading, `draft/voice_report.py`'s
  window line and edit headings. Still UTC on purpose, as sort/parse keys: backup
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
  target's deal price and a CVR's fair value are not): a drafter thread cites none
  (`target_problems`, from `check_hard_rules` per post and on its chart and table text,
  column headers included, and from `swarm/cells.py:cell_problems`), and a studio piece
  lists each in piece.json's `price_targets` (`qa.TARGET_FIELDS`: firm, target,
  `previous`, date, `rests_on`, the `catalyst`s the post says to watch, `in_model`
  yes/no/partly/unknown, `effect` unless yes, the firm's published `cases`, `post_says`,
  `source`), held to it by `studio/qa.py:check_price_targets` (fixable: a cited target
  with nothing listed, an entry missing a field, `post_says` in no post or card, a firm
  listed twice, a figure given as a target (a card's table of targets included,
  `qa.card_table_targets`) that no entry lists as its target, previous or
  case), while `studio/safety.py:advice_problems` blocks a target, fair value, value per
  share or computed change of the account's own (`_OWN_TARGET`, per-share figures only)
  in a post, on a card or in its alt text. Preprints are labelled as
  preprints. `draft/drafter.py:check_hard_rules` enforces all of this in code
  after generation (plus 280 chars/post with URLs as 23, the link ban,
  and verbatim-number verification); drafts that fail are stored as `failed`.
  Market context in dollars is the one exception to verbatim numbers (rule 4,
  `drafter.money_numbers`): a `$` figure or an amount in millions/billions may come from the
  model's knowledge, is listed in `claims_to_verify` and checked by step 2b; swarm cells let
  it through and `flag_unverified_numbers` skips a number an existing claim already carries.
- **Mentions and hashtags are a hard rule** (rule 11 in `draft/prompt.py:hard_rules`,
  mirrored by `draft/tags.py:tag_problems`, called from `check_hard_rules` per post and
  from `swarm/cells.py:cell_problems` per cell): an account whose X handle the story is
  given (`config.yaml`: `x:` on a `companies.feeds` / `branding.companies` entry, and the
  `mentions:` list of journals, societies and regulators with `domains:` and
  `match_names:`) must be written as @handle when a post names it, and a formal drug name
  (INN stem regex, `-cel` short names) or ClinicalTrials.gov number (`NCT` + 8 digits,
  `tags.nct_ids`) must be a hashtag, while a trial's name (KEYNOTE-189) stays plain text; a configured company name (`tags.company_names`, `drafter.known_company_names`)
  is never a drug, so Genmab is not `#Genmab`. Handles are never guessed: `drafter.story_handles` (`tags.load_handles` +
  `relevant_handles`: named in the source text, owning the URL host, or the
  `company_<key>` source) is the only list the model sees (`X HANDLES` in the user prompt,
  `Brief.handles` for the swarm) and the only one enforced. `numbers_in` ignores
  `@`/`#` tokens so an NCT number's digits are not a number to verify. Publish's re-check
  and human-approved texts are untouched.
- **No post carries a link** (`draft/hook.py:link_problems`, pure; rule 2 in
  `draft/prompt.py:hard_rules`, enforced per post from `check_hard_rules` and per cell from
  `swarm/cells.py:cell_problems`). Not the source URL, not a registry link, not a bare
  domain: X shows a post with an outbound link to fewer non-followers and posting a URL is
  billed as an extra request through the X API, so the source is named in words (the
  journal, the company, the meeting) with its @handle where rule 11 gives one. Nothing has
  to carry a URL, so `draft/schema.py:Format` normalises every non-thread shape to exactly
  one post (`SINGLE_SHAPE_POSTS`), `publish/thread.py:split_thread` neither requires a URL
  nor numbers that one post, and the prompts hand the model the primary source URL for
  reference only. `run_unlink.py` is the one-off operator pass over queued drafts written
  before the rule (`hook.strip_links`, pure: the link and the lead-in that introduced it
  come out, a post that was only a link is dropped; `store.edit` with the status unchanged,
  no model call; studio drafts are skipped), as `run_scrub_notes.py` is for captions. Both
  leave alone what `approval_queue/publishing.py:block_reason` says step 3 holds (live on X,
  posted, partial or claimed). Publish's re-check and human-approved texts are untouched.
- **Posts talk like a human** (`draft/style.py:style_problems`, pure; rule 13 in
  `draft/prompt.py:hard_rules`, enforced per post from `check_hard_rules` and per cell from
  `swarm/cells.py:cell_problems`). No colon (one between digits, 8:30 or 2:1, is fine) and
  no dash: em dash, en dash, `--` or a spaced ` - `. Publish's re-check and human-approved
  texts are untouched.
- **The first post is the hook** (`draft/hook.py:hook_problems`, pure; rule 12 in
  `draft/prompt.py:hook_rule`, enforced from `check_hard_rules` on `thread[0]` and from
  `swarm/cells.py:cell_problems` on a hook cell). It carries no thread position marker
  ("1/6"), no "thread" and no emoji, and it stays within `HOOK_MAX_CHARS`: X ranks a thread
  on its opening post, and an unanswerable summary there costs the rest of the thread its
  readers. A single or long post is its own opener and only the hook's length cap is lifted
  there (the format's `max_chars` applies).
- **KPIs are weighted, not counted.** `feedback/models.py:CONVERSATION_WEIGHTS` defines the
  derived `conversation` KPI (reply/quote x3, bookmark/repost x2, like x1, impression x0.05) beside the six
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
- **Draft images** (`draft/chart.py`): the drafter's `chart` is a chart
  SPEC (title, labels, values, unit, note), never a picture; `Chart.kind` is `bars` (the
  original), `grouped` (arms in `series` across endpoint labels) or `stat` (1-4 headline tiles
  with per-tile `units`), and a plain bar chart's JSON keeps its five keys.
  `chart.flat_chart_problems` sends back a bar chart whose bars are all equal, and
  `branding.story_logo` (source site via `Brand.domains`, else the title) is the
  `header_logo` drawn top right on every card; `suggested_visual` stays a
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
  `approval_queue/store.py:fetch_candidates` (one candidate per cluster: score at or
  above the bar within `--since-hours`, plus every story whose latest human feed rating is
  yes whatever its score or age, those first). **One story, one piece of writing**:
  `run_draft.py` skips a story the studio holds (`store.studio_held_clusters`, read-only
  on `studio_pieces` / `studio_topics`, empty when they are missing: a piece not
  discarded at any stage, or an unclaimed queued topic, a merged story followed through
  its `story_item`, and every story offered to a piece still researching on no story,
  `store.studio_researching_offers` over its `offered_stories`), looked at again before
  each story, and does not store a draft whose story a piece's research named meanwhile
  (`offered=False`); the studio's shortlist skips a story with a draft that did not
  fail (`store.drafted_cluster_ids`, followed through the draft's item), and research
  fails a piece whose named story got such a draft while it ran; the dashboard's
  feed-yes count (`ops/store.py:fetch_feed_yes_undrafted`) leaves the studio's out. Its own
  tables are `drafts`, `decisions`, `draft_examples` and `image_grades`; edits log original vs edited text.
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
  `fetch_draft_stats` (source and url); both leave studio drafts (`studio:%` item ids)
  out, so the drafter learns from its own drafts only.
- Step 2b (`verify/`) checks `claims_to_verify` against the web. Its only
  network call is `verify/verifier.py:call_model` (the CLI with
  `tools=["WebSearch","WebFetch"]` under `verify/config.yaml`'s own `timeout_seconds`;
  the image grader's `["Read"]` and the radar scan's own web tools are the only other
  `tools` lists passed to `claude_cli.run_claude`). It owns
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
  manual only (see the top of this file) and idempotent via the claim; partial threads
  are never retried automatically. `publish/thread.py` appends " (n/N)" to a thread's
  replies only under the shipped `thread_numbering: replies` in `publish/config.yaml`,
  keeping the opening post marker-free as rule 12 asks (`all` numbers it too, `none`
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
  voice-guide proposals compare the drafter's posts only (`suggest.NOT_A_FEED`,
  `_drafter_rows`).
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
  `python <AUTO_SCRIPTS>` (ingest, score, studio, draft, verify, feedback, evolve) with nothing
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
  pages they affect (feed: ingest + score; pending: draft, verify; each posts `step` and
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
  panel never writes `config.yaml`, `draft/voice.md` or a draft's text. The settings it
  edits itself are two keys of `publish/config.yaml` and two of `ops/config.yaml`
  (`auto_run_enabled`, `auto_run_times` through `ops/config.py:save_auto_run`, from
  `POST /runs/auto`; the step list stays file-only), and the included queue routes add
  `trusted_domains` in `verify/config.yaml`, above: `POST /publishing/caps` calls
  `publish/scheduler.py:save_caps` (`max_posts_per_day`, `min_gap_minutes`; line edits,
  comments kept). The dashboard's "Back up now" (`POST /backup`) calls `ops/backup.py:backup` into `backups.dir` with `backups.keep`, as `run_ops.py backup` does. It has no authentication: `run_app.py` binds localhost by default, and every POST must come from the app's own pages: `approval_queue/app.py:SameOriginOnly` (installed on the queue app and the panel's, so the queue's and the studio's routes too) answers 403 when `Origin`, or `Referer` without one, names another host than `Host` (or is `null`); a request with neither (tests, curl) passes. `/publishing` and
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
  `compare` is the jury against the control draft (ties go to the control). With `jury: human`
  (`swarm/config.yaml`, shipped) `compare` is skipped: `run_draft.store_for_pick` stores the
  swarm's draft in status `store.STATUS_CHOOSING` with the control's in `drafts.choice_json`
  (guarded migration; `store.set_choice`/`get_choice`, a coin flip for which is shown as A)
  and quick chart previews (`store.preview_file`); the queue's `/choose` pages show both
  blind and `approval_queue/choosing.py` (the queue's one door to `swarm/store.py`) resolves
  the pick: `store.resolve_choice` makes the row `pending` with the picked text,
  `swarm_store.record_human_pick` sets the run's winner, then the picture is drawn. Nothing
  downstream reads `choosing` (verify, the pending list and publish ask for `pending`);
  approve/edit/revise refuse it (409) and `ops/store.py` counts it as waiting. Every
  call takes `call=` and defaults to `draft.drafter.call_anthropic`; no new network
  module and no `claude-*` ID in code (`swarm/config.yaml` holds the cheap model).
  `run_draft.py:draft_with_swarm` stores the winner through `store.insert_draft`
  exactly as before, so verify, the queue, publish and feedback are unchanged;
  `swarm/store.py` owns `swarm_runs` / `swarm_variants` / `swarm_genomes` /
  `swarm_fitness`; its one read of another step's tables is `fetch_head_metrics`
  (step 3 `posts` + step 4 `tweet_metrics`, read-only, empty when missing).
  `run_draft.draw_genomes` picks each story's writer, designer and format by Thompson
  sampling on their credited scores (`evolve.allocation: thompson`,
  `swarm/store.py:thompson_next` over `fitness_scores`, pure `fitness.thompson_pick`;
  `inherited_priors` gives a child its parent's posterior, the parent's own prior being
  its parent's, halved every `evolve.dead_half_life` of the child's `dead_runs`, the runs
  that can never be credited to it); a kind with no scored post, or
  `allocation: round_robin`, rotates instead (`next_genome`, then `next_format` and
  `next_designer(writer_id=, format_id=)`, counting runs since the newest live genome of
  that kind was born so a child joins an even rotation; the seeds are
  `swarm/genome.py:SEED_GENOMES`; `swarm_runs.designer_id`; `images.attach_chart(style=)`
  is the Style the first render starts from, recorded on the draft by
  `approval_queue/store.py:set_style` (`drafts.style_json`, guarded migration) so every
  later render without a `style` (a revision, the verifier's table, a scrubbed caption)
  starts from it too, and `swarm_store.mark_styled` records that a chart, first or extra,
  was drawn in it). **A genome owns its topology**: `swarm/config.yaml` `fan_out`/`layers`
  are only the fallback for a row without them. `swarm/fitness.py` is pure (relative
  KPI `(value + evolve.smoothing) / (baseline + smoothing)` against the trailing median,
  per-genome scores keyed by `genome_id`, `designer_id` or `format_id`, `bet_summary`,
  `prune_confident` and the old median `prune`). **Fitness measures what a genome
  did**: `fetch_head_metrics` reads each head on its first snapshot at least
  `evolve.horizon_hours` old (younger posts are not scored and are nobody's baseline)
  and, with `evolve.subtract_self_reply`, without the thread's own post-2 reply;
  `run_evolve.credit` blanks `swarm_fitness.genome_id` when the control's text was posted
  or the format was a single post (`fitness.writer_credited`) and `designer_id` unless the
  chart was drawn in its Style (`swarm_runs.styled`, `designer_credited`), and `cmd_score`
  drops rows for runs it did not score (`keep_fitness`), so the report, breeding, pruning,
  allocation and the panel count a genome's own posts only. `evolve.prune_rule:
  confidence` retires a genome only when its mean log relative is below the pooled
  rest's with probability `1 - (1 - retire_confidence) / k` (a t test, per look; nothing
  while no genome has two posts), at most `max_retire_per_run` per kind per run; when
  that retires nobody, a genome with no credited post after `evolve.max_dead_runs` dead
  runs is retired instead (`run_evolve._never_credited`). `median` is the old coin-flip
  rule. `swarm/genome.py:asks_for_url` is the one URL check: `validate_child` applies it
  to a child's changed slots, and `ensure_tables` rewrites a live closer that still asks
  for the source URL (`store.closer_without_url`: the old seed rule becomes
  `CLOSER_RULE`, a reworded one loses only the URL clause). `swarm/mutate.py` breeds: `breed_writer` is
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
  needs_preprint=)`), and `assemble_prompt(..., fmt)` says the shape. Formats
  are pruned with `evolve.format_min_posts` (a coarse gene needs more posts) and bred by
  pure code (`mutate.breed_format`: one field stepped to a neighbour, named by
  `format_name`); `evolve.format_population_size` is their population.
  `tests/conftest.py` turns the swarm off for every test that does not opt in.
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
  `studio/exemplars`); `tools` and the isolation `cli_flags` (`--safe-mode
  --restricted --permission-mode dontAsk`) from `studio/config.yaml`; API keys stripped by
  `cli_env`). Stages (`studio/session.py`): research (`factbase.md`, `research.json`; its
  `story_id`, an int or digit string, sets the piece's cluster only when it is in
  `offered_stories`, the shortlist ids every research run of the piece was offered,
  recorded before the session starts so the drafter holds off them, and has no draft
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
  `DraftRow.studio_piece`), shape `long` at `x.long_post_max`, no claims (step 2b skips
  it), every card copied to `image_file(id, k)` and anchored to its post, each
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
  stories are quoted between `prompt.FEED_TEXT_START` / `FEED_TEXT_END` as data. `studio/runner.py` (run by `run_studio.py`) marks pieces left mid-stage as
  `interrupted`, acts on `request`s (continue, revise), then starts at most one piece:
  explicit `--topic`/`--story`, else the oldest queued topic, else with `--now` an
  automatic topic, else only when `auto.max_new_per_day` (automatic pieces per calendar
  day in the root `timezone:`), `auto.min_hours_between` (any piece) and no checkpoint
  wait allow, and a card browser was found (`make_renderer`; without one the automatic run
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
  `min_baseline_posts`), `arm_stats` per angle/shape/hook_style/cards bucket, `lean` (one
  Thompson draw per arm, normal posterior on the log scale, prior at the median; an angle
  the editor named is left out; None under `lean_min_measured`), `lean_shares` (the
  dashboard's share of draws), `evidence_block` (WHAT X SAYS, with a small-sample caveat),
  `rewrite_due` (new = scored pieces the last rewrite was not given, `unseen`), and
  `rewrite_prompt`/`parse_rewrite` (JSON, `REQUIRED_SECTIONS`, `max_words`).
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
  `propose`, shipped, waits for the editor to apply it, `auto` applies, `off` never; a failed call or a rejected
  reply changes nothing and exits 1). `studio/playbook.py:save` is the one writer of the
  playbook file (atomic) and of `studio_playbook_versions` (`seed`/`editor`/`learned`/
  `proposal`/`revert`, changelog, evidence, `pieces` learned from; `ensure_seeded`
  records the playbook in use before the first change, `revert`, `apply_proposal`).
  `/studio/performance` (`studio/dashboard.py`, pure views) shows the posted pieces, the
  per-arm table, the learning state and the version history, takes typed-in numbers and
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
draft/    schema.py (Draft, Format, validate_output), hook.py (rule 2: the link ban;
          rule 12: the opening post),
          chart.py (chart + table specs, verification, PNG rendering, Style
          knobs, 3D header, logos), grader.py (image grader: ImageGrade, CHECKLIST,
          grade_image, call_grader), branding.py (tickers + logos for company cells),
          logos.py (site icon discovery + PNG normalisation for run_logos.py), prompt.py,
          voice.md, drafter.py, config.yaml, settings.py, tags.py (Handle, load_handles,
          relevant_handles, trial_names, drug_names, tag_problems), targets.py
          (price-target citations: target_mentions, target_figures, target_problems,
          field_figure, SHARE_FIGURE),
          examples.py (EditExample, select_edit_examples, format_examples_block),
          voice_report.py (VoiceReport, build_report, render_markdown, CLI)
approval_queue/  store.py (drafts, decisions, draft_examples, fetch_candidates,
          fetch_decisions_for_voice, fetch_draft_stats, record_examples, image_dir,
          set_image, drop_image, image_grades), images.py (attach_chart, attach_table,
          render-grade loop), app.py (/voice,
          /drafts/{id}/image), templates/
panel/    views.py (pure view models, sparkline geometry), feed.py (scored feed +
          ratings), jobs.py (JobManager, background step runs, "Publish now", automatic mode),
          autorun.py (AutoRunner: the timer for everything but publishing), frozen.py (data dir,
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
assets/   logos/<company key>.png (human-supplied company logos for table cells)
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
          checks), repeats.py (pure: does a radar topic or catalyst repeat a piece),
          evidence.py (what X says, from the DB), playbook.py (the file, its
          versions, the rewrite), dashboard.py (pure views of /studio/performance),
          web.py + templates/ (/studio pages, /studio/radar)
run_ingest.py  run_score.py  digest.py  run_draft.py  run_verify.py  run_queue.py
run_app.py  run_desktop.py  pipeline_cli.py  run_publish.py  run_feedback.py  run_ops.py
run_logos.py  run_evolve.py  run_unlink.py  run_studio.py   (CLIs)
```
