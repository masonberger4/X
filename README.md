# Cancer Research X — AI-assisted pipeline

An AI pipeline that monitors immuno-oncology sources (PubMed, bioRxiv/medRxiv,
ClinicalTrials.gov, FDA, conference abstracts, ~50 company newsrooms), scores
new items, and drafts posts for an X account on the business and investing side
of immuno-oncology biotech: CAR-T and cell therapy, T-cell engagers and
bispecifics, trial readouts and catalysts for public companies, M&A and
financing. The AI writes as a PhD-level immuno-oncology analyst at a hedge fund
would. A human approves, edits, and adds commentary before anything is
published. Nothing it writes is medical or investment advice.

See [PLAN.md](PLAN.md) for the full design, principles, and build order, and
[prompts/](prompts/README.md) for the kickoff prompt that built each step.

| Step | What | Package / CLI |
|---|---|---|
| 1 | Ingest, dedup, prefilter, score, digest | `ingest/`, `filter/`, `score/`, `run_ingest.py`, `run_score.py`, `digest.py` |
| 2 | Draft posts, human approval queue | `draft/`, `approval_queue/`, `run_draft.py`, `run_queue.py` |
| 3 | Publish to X (dry run by default) | `publish/`, `run_publish.py` |
| 4 | Feedback loop: metrics, weekly report | `feedback/`, `run_feedback.py` |
| 5 | Operations: orchestrator, health, alerts, backups | `ops/`, `deploy/`, `run_ops.py` |
| 6 | Conference abstracts, KOL X list, HTTP retry | `ingest/crossref.py`, `ingest/x_list.py`, `ingest/http.py` |
| 7 | Voice learning loop from human edits | `draft/examples.py`, `draft/voice_report.py`, queue `/voice` |
| 8 | Control panel: one web app over the whole workflow | `panel/`, `run_app.py` |

## Control panel (step 8)

`python run_app.py` serves the whole workflow at http://localhost:8000:

| Page | What |
|---|---|
| `/` | health checks, the last outcome of every orchestrator step, row counts, database size, latest backup |
| `/sources` | every configured ingest source with its freshness, last error and item counts |
| `/feed` | the scored clusters `digest.py` prints, with its yes/no editor prompt and reason-category box inline |
| `/publishing` | approved and waiting, what has posted, and any partial thread needing a human |
| `/feedback` | follower trend, per-post metrics, and the latest report's proposals |
| `/runs` | start a run of any enabled step and watch its log, or stop the one in progress; recent runs with per-step output |
| `/queue`, `/drafts/{id}`, `/voice` | the step 2 approval queue, unchanged (its Revise box sends a draft back through the drafter with your note) |

`panel/` owns no tables. Every number comes from the read-only adapters in
`ops/store.py`, the pure checks in `ops/health.py`, and (for the feed page's ratings)
step 1's own `db.Database` API — the same one `digest.py` uses, and the run buttons execute
`ops/config.yaml`'s steps through `ops/runner.py` under the same `ops/lock.py` lock
cron takes, so a run started in the browser is the run cron would have started. A step
disabled in `ops/config.yaml` is skipped, never run: publishing stays off. The panel
never edits `config.yaml`, `draft/voice.md` or a draft's text, and has no publish
button. The feedback page renders a report's suggestions; applying one is still a human
editing a settings file and bumping `PROMPT_VERSION`. The one thing the panel writes
outside its own steps is a human yes/no decision on the feed page, through step 1's API, which
is exactly what `digest.py --rate` writes.

There is no authentication. Bind it to localhost and reach it over an SSH tunnel or a
private network; the run buttons execute the pipeline's CLIs.

`run_queue.py` still serves the approval queue on its own for anyone who wants only
that.

### Desktop build

`run_desktop.py` opens the same server in a native window (pywebview, the Edge engine on
Windows) and stops it when the window closes; run it with `pythonw` and no console appears.
`deploy/desktop.spec` turns that into a folder with `Pipeline.exe` and `pipeline-cli.exe`
via PyInstaller. Both extras live in `pip install -e ".[desktop]"`; the pipeline itself never
needs them.

How the frozen build stays honest with the rest of the code: every settings file and
template ships inside the bundle at its usual relative path, so each step's
`Path(__file__)` lookups are unchanged; the database, `.env`, lock and backups live beside
the exe, which the launcher makes the working directory; and since there is no python.exe,
the runs page launches each step as `pipeline-cli.exe run_ingest.py ...`, where
`pipeline_cli.py` maps the script name to its module. `panel/frozen.py:bundle_manifest` is
the one list of what gets bundled, and a test checks it against the files on disk.

## Principles

- Human-in-the-loop by default.
- Every post adds interpretation, never just description.
- No medical advice or treatment recommendations, ever.
- No investment advice: no buy/sell/hold/short calls, price targets or return
  promises. Implications and risks, yes; the reader decides.
- Always link the primary source; label preprints as preprints.
- Never fabricate numbers.

**Plain-English overview:** [OVERVIEW.md](OVERVIEW.md) explains what the
project does and why, no code. **Step-by-step operator guide:**
[HOWTO.md](HOWTO.md) has every command in
order, from install to scheduled publishing, with Windows commands.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env        # add ANTHROPIC_API_KEY (and optionally NCBI_API_KEY)
ruff check . && ruff format --check . && pytest
```

Edit `config.yaml` to change feeds, PubMed queries, company list, keywords,
cadences, the score threshold, or the scoring model.

### Windows

Everything runs on Windows too (CI tests it). Use `python` instead of
`python3`, `copy` instead of `cp`, and `.venv\Scripts\activate` to enter the
virtual environment. Two Windows-only details are handled for you: the
`tzdata` package supplies the time zone database Windows lacks, and the
orchestrator's single-instance lock uses a Windows file lock. To run the
pipeline on a schedule, use Task Scheduler (see `deploy/README.md`) instead of
cron or systemd.

## Run

```bash
python run_ingest.py            # fetch every source that is due by cadence
python run_ingest.py --force    # ignore cadence
python run_ingest.py --source pubmed_oncology -v
python run_score.py             # prefilter + score unscored clusters
python run_score.py --dry-run   # see what would be scored
python run_score.py --refilter  # after editing prefilter keywords: re-evaluate earlier drops
python run_score.py --no-link   # skip the story-linking model call (config `linking:`)
python digest.py                # top-N clusters of the last 24h as markdown
python digest.py --all --hours 72 --out digest.md
python digest.py --rate         # yes/no per entry plus a required explanation (saved to `ratings`)
python digest.py --auto-rate    # models.rater (config.yaml) answers the same yes/no; shown in --rate
python digest.py --auto-rate --rate   # model first, then you, with its decision as a hint
python run_draft.py             # draft approved candidates
python run_verify.py            # check each draft's claims against the web (step 2b)
python run_queue.py             # approval UI alone on localhost:8000
python run_queue.py --host 0.0.0.0 --port 8080   # bind elsewhere (--reload for development)
python run_app.py               # control panel: dashboard + sources + runs + the queue
python run_app.py --host 0.0.0.0 --port 8080     # bind elsewhere (--reload for development)
pythonw run_desktop.py          # the same panel in a native window, no console (pip install -e ".[desktop]")
pyinstaller deploy/desktop.spec # build dist/Pipeline: Pipeline.exe + pipeline-cli.exe, no Python needed
python pipeline_cli.py run_ops.py status   # what the exe runs steps with; works from a checkout too
python run_publish.py           # DRY RUN (default): print what would post and when
python run_publish.py --live    # posts only if PUBLISH_ENABLED=1 is also set
python run_publish.py --live --breaking   # only FDA / company-approval items
python run_publish.py --live --now        # ignore slots, post the top candidate once
```

Common flags: `--config PATH` picks another root `config.yaml` (`run_ingest`,
`run_score`, `digest`, `run_publish`, `run_feedback`; `run_ops` also takes
`--db PATH`), `digest.py --top N` sets how many entries print, and `-v` turns
on DEBUG logging everywhere.

Suggested cron: `run_ingest.py` every 30 min, `run_score.py` hourly, read
`digest.py` daily, `run_publish.py --live` every 15 min, `run_feedback.py
snapshot` daily. Or let `run_ops.py run` drive the whole sequence (step 5).

## How it works

1. **Ingest** (`run_ingest.py`): each source in `config.yaml` has a `type`
   (`rss`, `biorxiv`, `pubmed`, `clinicaltrials`, `fda_oce`, `crossref`,
   `x_list`) and a `cadence_minutes` (optionally overridden by meeting
   `windows`, step 6). Items are normalised into `Item` (pydantic) with a
   `dedup_hash` of normalised title+url.
2. **Dedup / cluster** (`filter/dedup.py`): exact hash -> DOI -> near-duplicate
   title. One cluster per story; a cluster records every source that covered it.
3. **Prefilter** (`filter/prefilter.py`): keyword allow/deny, short-abstract
   drop, daily cap. Cheap and deterministic; runs before any API call. A source
   may set its own `min_abstract_chars` (the trade-press feeds do: their items
   carry a one-line summary); a cluster is held to the lowest floor among its
   sources.
3b. **Link** (`filter/link.py`): one model call (`models.linker`, settings in
   `linking:`) over the passed clusters of the last `window_hours`, scored or
   not, returns groups that report the same event: a release, its wire copy,
   trade-press write-ups with their own headlines. Code validates the groups
   and `Database.merge_clusters` folds each into one cluster, keeping the one
   that already has a score so the story is not scored twice. A failed call is
   logged and scoring goes on unmerged; `--no-link` skips it.
4. **Score** (`score/`): batches of ~10 clusters go to the model named in
   `models.scorer` via tool use with a strict JSON schema. Each row stores the
   model, prompt version, raw response, five 0-10 dimensions, evidence level,
   hype risk, rationale and suggested angle. `total` = sum of dimensions minus
   half the hype risk (0-50).
5. **Digest** (`digest.py`): top clusters above `scoring.threshold` in the
   window, as markdown. `--rate` collects the editor's yes/no decisions and
   explanations (reason categories in `score/editorial.py`) for rubric tuning.

## Draft images (charts)

The drafter's `suggested_visual` is a one-line idea for the reviewer. The
image that actually ships is a **chart the model specifies and code renders**
(`draft/chart.py`): the output JSON has an optional `chart` (title, labels,
values, unit, note; `null` when the source has no comparable numbers). The
Anthropic API draws nothing, and a picture the pipeline cannot audit would
break "never fabricate numbers", so:

- every number in the chart (values, title, labels, note) is checked verbatim
  against the source like the post text (`drafter.verify_chart`); one miss and
  the chart is dropped and a low-confidence claim says which number
  (`drop_unverified_chart`). The text is unaffected;
- `run_draft.py` renders the surviving spec with matplotlib (`pip install -e
  ".[images]"`; without it, or with `images: enabled: false` in
  `draft/config.yaml`, drafts are stored without an image) to
  `<db folder>/images/draft_<id>.png` (`approval_queue/images.py:attach_chart`,
  fail-soft). `drafts.chart_json` and `drafts.image_path` are guarded
  migrations in `approval_queue/store.py`;
- the queue shows the PNG and its alt text at `/drafts/{id}/image`; "Drop
  image" (`POST /drafts/{id}/image/drop`) clears both and logs an `edit`
  decision with the text unchanged; a revise re-renders from the new draft;
- `run_publish.py` attaches it to the first post (`publish/client.py:
  upload_media`, v1.1 media upload plus alt text, then `create_tweet` with
  `media_ids`). `media: attach_images: false` in `publish/config.yaml` posts
  text-only. An upload failure marks the draft `failed` with nothing posted.

## Claim verification (step 2b)

`draft/` flags every fact the model added from its own knowledge as a claim to
verify. `run_verify.py` sends each claim to Claude with web search enabled
(`verify/verifier.py:call_model`, the only network call: the CLI with
`--tools WebSearch,WebFetch` on the `claude_code` backend, the server-side
`web_search` tool on the API) and stores a verdict (`supported`,
`contradicted`, `unverified`), the source URL, the verbatim sentence and a note
in its own table `claim_checks` (`verify/store.py`). A verdict counts as
verified only when the source host is in `verify/config.yaml`
`trusted_domains` or is a company feed host from the root config; otherwise
it is shown as a lead. The queue shows the evidence beside each claim and
refuses Approve with 409 while any claim is contradicted, unless the form
carries `override=1` ("approve anyway"). The verifier never edits a draft.
Fixing a draft is the drafter's job, on request: the queue's Revise action
(`POST /drafts/{id}/revise`, `draft/drafter.py:revise_item`) re-prompts the
model with the current draft, the human's instructions and every claim check
that came back contradicted or unverified, re-runs the hard rules in code,
replaces the draft in place (still pending, logged as a `revise` decision
with the before/after text) and drops its claim checks so the next
`run_verify.py` checks the new claims.
`ops/config.yaml` runs it after `draft` as an optional step.

```bash
python run_verify.py             # pending drafts with unchecked claims
python run_verify.py --dry-run   # list, no calls
python run_verify.py --redo      # replace earlier verdicts
```

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
- A draft's chart image (see "Draft images") goes on the first post via
  `client.upload_media`; the upload happens before any tweet, so a failed
  upload posts nothing.

### Bio disclosure (manual)

PLAN.md requires the account bio to disclose AI-assisted drafting. Editing
the bio is deliberately **not** automated. Before the first live run, edit the
bio on X by hand, then set `BIO_DISCLOSURE_CONFIRMED=1` in `.env`;
`run_publish.py` logs a warning at startup until it is set.

## Feedback loop (step 4)

`run_feedback.py` pulls `public_metrics` for every tweet step 3 published,
stores daily/weekly snapshots, and renders a weekly markdown report that says
which sources, formats, slots and topics perform, plus concrete suggestions
for the rubric and prefilter. It is **read-only** against X (app-only auth,
`X_BEARER_TOKEN` in `.env`) and never posts, edits or deletes anything.

```bash
python run_feedback.py snapshot             # metrics for tweets that are due + followers
python run_feedback.py snapshot --dry-run   # print the ids it would fetch
python run_feedback.py snapshot --all       # ignore the schedule (still once per day)
python run_feedback.py report               # last week, markdown to stdout, no network
python run_feedback.py report --weeks 4 --out report.md
python run_feedback.py followers            # follower time series
```

Suggested cron: `snapshot` once a day, `report --out` once a week. Rerunning
`snapshot` the same day fetches nothing. Settings (username, snapshot schedule,
KPI, minimum posts per group, topic keywords, rate-limit wait) live in
`feedback/config.yaml`, not the root config. Tables: `tweet_metrics`,
`follower_snapshots`, `feedback_reports`.

Caveat: the API exposes `public_metrics` only. The Original Content Rewards
"Premium impressions" figure is not available, so `impression_count` (all
viewers) is a proxy. Groups below `min_posts_per_group` are flagged small-n
and never produce a suggestion.

The report **proposes** changes and applies none. A human edits
`score/rubric.py` (weights in `compute_total`, few-shot anchors; then bump
`PROMPT_VERSION` so `run_score.py` re-scores), `config.yaml` (prefilter
keywords, source cadences), `publish/config.yaml` (slots, `post_format`) or
`draft/voice.md`, as each suggestion names.

## Operations (step 5)

`run_ops.py` runs the whole pipeline unattended under cron or a systemd timer,
detects when a source or stage has silently stopped, backs up the database, and
tells you when something needs attention. It never posts, never calls the
Anthropic API, and never edits content; it runs the other CLIs as subprocesses.

```bash
python run_ops.py run                 # lock; ingest -> score -> draft [-> publish -> feedback]
python run_ops.py run --only ingest   # a subset
python run_ops.py run --dry-run       # print the argv per step, run and record nothing
python run_ops.py health [--json] [--alert]   # checks; exit 1 if anything is 'fail'
python run_ops.py backup [--keep N]   # verified SQLite online backup into backups/
python run_ops.py status              # last run per step, last health, row counts, backup age
python run_ops.py prune --days 90     # ops-owned tables only (pipeline_runs, health_checks, alerts_sent)
```

Settings live in `ops/config.yaml` (step order, per-step `timeout_seconds` where 0 means
no limit, as `verify` uses, health thresholds and budget caps, backup dir/keep, alert channels and cooldown). The `publish` step is
disabled there and its argv is the dry-run default; enable it and add `--live`
yourself, together with `PUBLISH_ENABLED=1`, after reading the publishing section
above. Steps whose CLI has not merged yet are skipped with a warning.

Health checks: sources (error / never ran / stale), staleness of ingest, score
and draft, unscored backlog and pending-draft age, per-day scoring and drafting
budget, publish `partial`/`failed`/stuck claims, feedback snapshots, backup age,
DB size and disk free, and required env var names (never values). Alerts go to
the log always, and optionally to a webhook (`ALERT_WEBHOOK_URL`, works for
Slack/Discord/Mattermost incoming webhooks) or email (`SMTP_*`,
`ALERT_EMAIL_FROM/TO`). A check that keeps failing is re-sent only after
`alerts.cooldown_hours`; a recovery sends one message. Alerts carry check names,
summaries and counts only.

Deploy files: `deploy/crontab.example`, `deploy/pipeline.service`,
`deploy/pipeline.timer`, and `deploy/README.md` (VPS setup, lock/backup/log
locations, how to restore a backup).

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

## Voice learning (step 7)

Every edit and rejection in the approval queue is training data. Step 7 reads
it back: `run_draft.py` turns recent human edits into BEFORE/AFTER few-shot
examples in the drafting prompt, the queue records **why** a draft was edited
or rejected (`voice`, `factual`, `not_newsworthy`, `hard_rule`, `other`), and
a voice report tells you which `draft/voice.md` changes the edits are asking
for. Nothing posts; the Anthropic API is called only where step 2 already
calls it, with a longer system prompt.

```bash
python run_draft.py                     # examples on by default (draft/config.yaml)
python run_draft.py --no-examples       # plain prompt, exactly as before step 7
python run_draft.py --dry-run           # prints block length + example decision ids
python -m draft.voice_report            # last 4 weeks, markdown to stdout, no network
python -m draft.voice_report --weeks 8 --out voice.md
python -m draft.voice_report --json     # same data as JSON
python -m draft.voice_report --examples # the exact block the next run_draft sends
```

The approval UI (`run_queue.py`) gets a "why" select on the edit and reject
forms, a before/after diff under each edit in the decision history, and a
**Voice report** page at `/voice?weeks=N`.

How examples are chosen (`draft/config.yaml`, section `examples`): only
`edit` decisions from the last `lookback_days` whose text actually changed by
at least `min_change_ratio`, whose category is not in `skip_categories`
(factual and hard-rule fixes are not voice lessons), and whose **edited** text
passes `check_hard_rules` for its own URL and source. An edit that slipped in
advice or dropped the link is logged as a WARNING and never taught. Newest
first, at most `max_examples` pairs and `max_rejections` rejected posts, each
post cut at `max_chars_per_post`. The block is built once per run, goes into
the system prompt after the voice guide and before the hard rules, and every
output is still checked in code: examples can never relax a rule. Table
`draft_examples` records which decisions each draft was shown.

The report **proposes** and applies nothing. Each proposal names the
`draft/voice.md` section to paste into: a phrase you deleted
`propose_banned_after` times becomes a proposed banned phrase, a median
single-post length change below -40 chars means posts run long, a thread cut
in more than half of the edits means lead with the single post, and a note
word such as "hype" or "jargon" recurring three times is a tone proposal. Edit
`voice.md` by hand; the next run picks it up.

Drafting model resolution: `DRAFT_MODEL` in `.env`, else `models.drafter` in
the root `config.yaml` if you add that key, else `model` in
`draft/config.yaml`. Databases created before step 7 are migrated in place
(`decisions.category` is added with a guarded `ALTER TABLE`).

## Headless backend (optional)

By default the scorer and drafter call the Anthropic API with `ANTHROPIC_API_KEY`
(metered, pay as you go). As an alternative for personal, low-volume use, the
same two call sites can run the Claude Code CLI in print mode instead, so the
calls are covered by whatever account the CLI is logged in with:

```yaml
# config.yaml
models:
  backend: claude_code     # default: api
claude_code:
  binary: claude           # must be on PATH and logged in (`claude login`)
  timeout_seconds: 300
  extra_args: []
```

or `LLM_BACKEND=claude_code` in `.env` (the env var wins). Nothing else changes:
`run_score.py` and `run_draft.py` behave the same, the score rows record
`backend: claude_code` in `raw_response`, and every draft still goes through
`check_hard_rules`.

How it works: `claude_cli.run_claude` launches
`claude -p --output-format json --bare --tools "" --model <model>` with the
system prompt as an argument and the user prompt on stdin, reads the JSON
envelope, and returns the reply text. The scorer, which normally relies on a
strict tool schema, instead inlines that schema into the system prompt and
validates the reply in code: a malformed reply fails the batch (logged, skipped,
picked up next run); a CLI failure (not logged in, rate limited, timeout) is
retried with the same backoff as an API error. A safeguard verdict (`safeguards
flagged this message`) is `ClaudeCliRefused`: it is deterministic for a given prompt,
so it is never retried; instead `Scorer.score_batch_splitting` halves the batch and
scores each half in its own call, down to single clusters, and a cluster refused on
its own is logged and left unscored. `models.scorer_effort`, `models.drafter_effort`,
`models.rater_effort` and `verify/config.yaml:effort` set the effort level per phase
(`--effort` on the CLI, `output_config.effort` on the API; blank means the model's
default).

Trade-offs, so you can decide with eyes open:

- The subscription's rolling usage limits are shared with your own Claude Code
  sessions; a limit hit stalls scoring until it resets.
- The machine running cron needs Claude Code installed and kept logged in.
- Reply shape is requested, not enforced; expect an occasional skipped batch.
- Anthropic's terms treat consumer subscriptions as covering their own products,
  not programmatic use. A scheduled pipeline sits in a grey area; the API backend
  is the clearly supported path and costs roughly $5-15/month at this volume.

## Database

SQLite (`db_path` in config). Tables: `items`, `clusters`, `scores`,
`ratings`, `source_runs` (step 1); `drafts` (with `chart_json` and
`image_path` for the rendered chart in `<db folder>/images/`), `decisions`,
`draft_examples` (steps 2 and 7); `schedule`, `posts` (step 3); `tweet_metrics`,
`follower_snapshots`, `feedback_reports` (step 4); `pipeline_runs`,
`health_checks`, `alerts_sent` (step 5). Every score is kept, so re-scoring
after a prompt change is additive. Each step creates only its own tables and
reads the others through adapter functions in its `store.py`.

## Known source caveats

- EurekAlert no longer publishes RSS; bioRxiv/medRxiv RSS was retired, so
  those use the `api.biorxiv.org` JSON API.
- `fda.gov` (OCE approvals page) and `clinicaltrials.gov` sit behind bot
  protection that blocks some cloud egress. Both sources fail soft; run from a
  residential/VPS IP or add a proxy if they return 401/403.
- Several big-pharma newsrooms have no public feed or block bots; the company
  list in `config.yaml` contains only feeds verified to work.

