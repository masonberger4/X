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
| 9 | Swarm drafting: many cheap cells against the single drafter | `swarm/`, `run_evolve.py` |
| 10 | The studio: one Opus 5.5 session per post (research, post, cards, fact-check) | `studio/`, `run_studio.py` |

## Control panel (step 8)

`python run_app.py` serves the whole workflow at http://localhost:8000:

| Page | What |
|---|---|
| `/` | health checks, the last outcome of every orchestrator step, row counts, database size, latest backup |
| `/sources` | every configured ingest source with its freshness, last error and item counts |
| `/feed` | the scored clusters `digest.py` prints, with its yes/no editor prompt and reason-category box inline; one "Ingest and score" button |
| `/publishing` | approved and waiting, what has posted, any partial thread needing a human, a form for `max_posts_per_day` / `min_gap_minutes` (written into `publish/config.yaml` by `publish/scheduler.py:save_caps`, comments kept); no post button and no automatic publishing: posting is manual only, from the approved page's "Publish now" |
| `/feedback` | follower trend, per-post metrics, and the latest report's proposals |
| `/runs` | the automatic runs (switch and times of day, next run, what happened at each time), every run's log (whichever page started it) and the checkboxes to run any enabled step; stop any run in progress |
| `/queue`, `/drafts/{id}`, `/voice` | the step 2 approval queue (its Revise box sends a draft back through the drafter with your note); the pending page has "Draft" and "Verify" buttons |
| `/status/approved` | the waiting list with "Publish now" per draft (`run_publish.py --live --now --draft ID`, still gated by `PUBLISH_ENABLED=1`), "Set schedule" to number the order the slots post them (`schedule.position`), and "Reopen" to send a draft that has not gone out back to pending (`POST /drafts/{id}/reopen`; refused for a posted, partial or claimed draft) |

`panel/` owns no tables. Every number comes from the read-only adapters in
`ops/store.py`, the pure checks in `ops/health.py`, and (for the feed page's ratings)
step 1's own `db.Database` API — the same one `digest.py` uses, and the run buttons execute
`ops/config.yaml`'s steps through `ops/runner.py` under the same per-step `ops/lock.py`
locks cron takes, so a run started in the browser is the run cron would have started. While
it is open the panel also runs everything but publishing on its own: `auto_run_steps`
(ingest, score, draft, verify, feedback, evolve) at each of `auto_run_times` (01:00, 03:00,
06:00, 09:32, 12:00 and 15:00 shipped, in the root `timezone:`), switched and timed from `/runs`, which writes
`auto_run_enabled` / `auto_run_times` in `ops/config.yaml`. Only those six scripts can
ever start that way, and those runs cannot post whatever `.env` says. A step
disabled in `ops/config.yaml` is skipped, never run; the shipped `publish` step runs
`run_publish.py` as a dry run (no `--live`), and posting is manual only: nothing posts on a
schedule, and `run_ops.py run` refuses any configured step that carries `--live`. The run buttons sit on the pages they affect (feed, pending, approved)
and every log stays on `/runs`. The one argv the panel builds itself is the approved
page's "Publish now" (`panel/jobs.py:start_publish_now`): `run_publish.py --live --now
--draft ID` for the draft the human pointed at, which still posts nothing unless
`PUBLISH_ENABLED=1` is set. "Set schedule" on the same page writes the human's order to
step 3's `schedule.position` through `publish/store.py:set_order`; the scheduler posts
ordered drafts first. The panel
never edits `config.yaml`, `draft/voice.md` or a draft's text. The feedback page renders a report's suggestions; applying one is still a human
editing a settings file and bumping `PROMPT_VERSION`. The one thing the panel writes
outside its own steps is a human yes/no decision on the feed page, through step 1's API, which
is exactly what `digest.py --rate` writes.

There is no authentication. Bind it to localhost and reach it over an SSH tunnel or a
private network; the run buttons execute the pipeline's CLIs. So that a web page open in
the same browser cannot press them, every POST (on `run_app.py` and `run_queue.py` alike)
must come from the app's own pages: one whose `Origin`, or `Referer` when it has no
`Origin`, names another host than the one it was sent to (or `Origin: null`) is refused
with 403 before any route runs (`approval_queue/app.py:SameOriginOnly`). A request with
neither header (curl, a script on the machine) goes through.

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
- No investment advice: no buy/sell/hold/short calls, no price target of the
  account's own, no return promises. Implications and risks, yes; the reader
  decides. An analyst's published target is cited only with what it rests on,
  whether the catalysts the post says to watch are in it and which way they would
  move it (a studio piece; a drafter thread, chart or table cites none).
- Name the primary source in words (no post carries a link); label preprints as
  preprints.
- Never fabricate numbers.

**Plain-English overview:** [OVERVIEW.md](OVERVIEW.md) explains what the
project does and why, no code. **Step-by-step operator guide:**
[HOWTO.md](HOWTO.md) has every command in
order, from install to scheduled publishing, with Windows commands.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env        # optional NCBI_EMAIL / NCBI_API_KEY; nothing for Claude
npm install -g @anthropic-ai/claude-code && claude login   # every model call runs this CLI
ruff check . && ruff format --check . && pytest
```

Every model call runs through the Claude Code CLI, logged in with your own
account; there is no API key to set, and an `ANTHROPIC_API_KEY` left in `.env`
is ignored (see [Claude Code CLI](#claude-code-cli)). The tests replace every
model call, so `pytest` runs without the CLI.

Edit `config.yaml` to change feeds, PubMed queries, company list, keywords,
cadences, the score threshold, the scoring model, or `timezone:` (see
[Display time zone](#display-time-zone)).

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
python run_studio.py            # step 10: act on studio requests, start a piece if the limits allow
python run_studio.py --topic "next-gen CTLA-4" --checkpoint   # a studio piece now, stop after research
python run_studio.py --story 123 --angle deal_decoder        # from feed story 123, at this angle
python run_studio.py --now --no-checkpoint   # a piece now on today's best story, straight through
python run_studio.py --resume-only   # only Continue / Revise / Resume what the studio page asked for
python run_studio.py --list      # recent pieces and queued topics
python run_studio.py --dry-run   # print the research prompt the next piece would get
python run_studio.py --scan      # the radar's daily news scan (topics and catalysts), when due
python run_studio.py --scan-now  # the scan now
python run_studio.py --learn     # score posted pieces against X; rewrite the playbook when due
python run_studio.py --learn-now # the same, rewriting the playbook now
python run_queue.py             # approval UI alone on localhost:8000
python run_queue.py --host 0.0.0.0 --port 8080   # bind elsewhere (--reload for development)
python run_app.py               # control panel: dashboard + sources + runs + the queue
python run_app.py --host 0.0.0.0 --port 8080     # bind elsewhere (--reload for development)
pythonw run_desktop.py          # the same panel in a native window, no console (pip install -e ".[desktop]")
pyinstaller deploy/desktop.spec # build dist/Pipeline: Pipeline.exe + pipeline-cli.exe, no Python needed
python pipeline_cli.py run_ops.py status   # what the exe runs steps with; works from a checkout too
python run_publish.py           # DRY RUN (default): print what would post and when
python run_publish.py --live    # by hand only; posts only if PUBLISH_ENABLED=1 is also set
python run_publish.py --live --breaking   # only FDA / company-approval items
python run_publish.py --live --now        # ignore slots, post the top candidate once
```

Common flags: `--config PATH` picks another root `config.yaml` (`run_ingest`,
`run_score`, `digest`, `run_publish`, `run_feedback`; `run_ops` also takes
`--db PATH`), `digest.py --top N` sets how many entries print, and `-v` turns
on DEBUG logging everywhere.

Suggested cron: `run_ingest.py` every 30 min, `run_score.py` hourly, read
`digest.py` daily, `run_feedback.py snapshot` daily; never schedule
`run_publish.py --live`, posting is manual only. Or let `run_ops.py run` drive the whole sequence (step 5).

## How it works

1. **Ingest** (`run_ingest.py`): each source in `config.yaml` has a `type`
   (`rss`, `biorxiv`, `pubmed`, `clinicaltrials`, `fda_oce`, `crossref`,
   `x_list`) and a `cadence_minutes` (optionally overridden by meeting
   `windows`, step 6). Items are normalised into `Item` (pydantic) with a
   `dedup_hash` of normalised title+url.
2. **Dedup / cluster** (`filter/dedup.py`): exact hash -> DOI -> near-duplicate
   title. One cluster per story; a cluster records every source that covered it.
3. **Prefilter** (`filter/prefilter.py`): keyword allow/deny, short-abstract
   drop, daily cap. Cheap and deterministic; runs before any model call. A source
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
   `models.scorer` through the Claude Code CLI, with the scoring tool's strict
   JSON schema (`score/rubric.py:TOOL`) in the system prompt and the reply
   checked in code. Each row stores the
   model, prompt version, raw response, five 0-10 dimensions, evidence level,
   hype risk, rationale and suggested angle. `total` = sum of dimensions minus
   half the hype risk (0-50).
5. **Digest** (`digest.py`): top clusters above `scoring.threshold` in the
   window, as markdown. `--rate` collects the editor's yes/no decisions and
   explanations (reason categories in `score/editorial.py`) for rubric tuning.

## No links in posts

No post the pipeline writes carries a link of any kind — not the primary source
URL, not a registry link, not a company page. X shows a post with an outbound
link to fewer non-followers, and posting a URL is billed as an extra request
through the X API, so the source is named in words (the journal, the company,
the meeting) with its @handle where the pipeline knows one. It is hard rule 2:
`draft/hook.py:link_problems` (URLs and bare domains) is applied per post by
`drafter.check_hard_rules` and per cell by `swarm/cells.py:cell_problems`, so a
draft that writes one is retried and, if it keeps writing one, stored as
`failed`. Rule 12 (`draft/hook.py:hook_problems`) adds the opener's own rules on
top: no "1/6", no "thread", no emoji, and at most `HOOK_MAX_CHARS` characters
for a thread's first post. Because nothing has to carry a URL, a single or long
format is exactly one post. Drafts written before this rule are cleaned by
`python run_unlink.py` (`--status STATUS`, `--dry-run`, `-v`), the one-off pass
that strips the link and its lead-in from every queued draft's posts as an
`edit` decision, with no model call and no network.

## Draft images (charts)

Since step 9 phase four a draft may carry zero, one or two pictures, each
anchored to a post (`drafts.images_json`); what follows describes the first
picture, which every older reader still finds in `image_path`.

The drafter's `suggested_visual` is a one-line description for the reviewer. The
image that actually ships is a **chart the model specifies and code renders**
(`draft/chart.py`) or a table (below): the output JSON must carry exactly one of
`chart` (title, labels, values, unit, note) and `table`; an output with neither
fails the schema check and the drafter retries, so every draft comes with a
visual. The model draws nothing, and a picture the pipeline cannot audit
would break "never fabricate numbers", so:

- every number in the chart (values, title, labels, note) is checked verbatim
  against the source like the post text (`drafter.verify_chart`); one miss is a
  retry reason like a hard-rule violation (`drafter.chart_problems`), and a draft
  that never gets it right is stored as `failed` with the missing numbers;
- `run_draft.py` renders the surviving spec with matplotlib (`pip install -e
  ".[images]"`; without it, or with `images: enabled: false` in
  `draft/config.yaml`, drafts are stored without an image) to
  `<db folder>/images/draft_<id>.png` (`approval_queue/images.py:attach_chart`,
  fail-soft). `drafts.chart_json` and `drafts.image_path` are guarded
  migrations in `approval_queue/store.py`;
- every render is graded by a second model (`draft/grader.py`, settings under
  `images: grader:` in `draft/config.yaml`): 1-10 on readability, use of
  colour and graphics, and limited negative space, with flaws and fixes. Under
  `min_score` (8) the renderer applies the grader's layout-knob changes
  (`draft/chart.py:Style`: text scale, bar thickness, row pitch, highlight,
  gridlines, track) and draws again, up to `max_iterations` (4) renders; the
  best-scoring render is kept. Grades live in `image_grades` and show on the
  draft page, together with a professional-finish checklist (readability at
  thumbnail size, hierarchy, alignment, header finish, branding cells, number
  format, source footer, consistency) scored 1-10 each. The grader never
  touches a number, label or title;
- tables carry company branding (`draft/branding.py`): a company cell that
  names a configured company gets "($TICKER)" from `ticker:` in `config.yaml`
  (`companies.feeds` or `branding.companies`) and the logo from
  `assets/logos/<key>.png`. `python run_logos.py` fills that folder from each
  company's own site icon (apple-touch-icon, else favicon; `draft/logos.py`
  picks and normalises, `ingest/http.py` fetches) for a human to review;
  `--only KEY`, `--force`, `--dry-run`. The pipeline itself fetches nothing
  and an unconfigured company is left as written. The header row is a
  rounded navy bar with a drop shadow and sheen;
- a picture's footnote is a caption for the reader (n, design, as-of date), never
  an instruction to the operator: `draft/chart.py:note_problems` fails such a caption
  at drafting time, and `python run_scrub_notes.py` (`--status STATUS`, `--dry-run`,
  `-v`) clears it from drafts made before that rule and redraws their pictures;
- the queue shows the PNG and its alt text at `/drafts/{id}/image`; "Drop
  image" (`POST /drafts/{id}/image/drop`) clears both and logs an `edit`
  decision with the text unchanged; `POST /drafts/{id}/image/{index}/drop`
  ("Drop this picture") drops just that one of a two-picture draft, with the
  chart or table behind it, and moves the pictures after it down a place, so
  index 0 stays what `drafts.image_path` names; a revise re-renders from the
  new draft;
- `run_publish.py` attaches it to the first post (`publish/client.py:
  upload_media`, v2 media upload plus alt text, then `post_tweet` with
  `media_ids`). `media: attach_images: false` in `publish/config.yaml` posts
  text-only. An upload failure marks the draft `failed` with nothing posted.

## Claim verification (step 2b)

`draft/` flags every fact the model added from its own knowledge as a claim to
verify. `run_verify.py` sends each claim to Claude with web search enabled
(`verify/verifier.py:call_model`, the only network call: one Claude Code CLI run
with `--tools WebSearch,WebFetch`, under `verify/config.yaml`'s own
`timeout_seconds`) and stores a verdict (`supported`,
`contradicted`, `unverified`), the source URL, the verbatim sentence and a note
in its own table `claim_checks` (`verify/store.py`). A verdict counts as
verified only when the source host is in `verify/config.yaml`
`trusted_domains` or is a company feed host from the root config; otherwise
it is shown as a lead with a "trust <host>" button (`POST /drafts/{id}/trust`)
that adds the host to `trusted_domains` (`verify/settings.py:add_trusted_domain`,
a line edit), marks the stored verdicts from it trusted
(`verify/store.py:mark_host_trusted`) and redraws the draft's table
(`verify/render.py:finalize_table`) without a web call. The queue shows the evidence beside each claim and
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
`--auto-revise` (or `auto_revise: enabled: true` in `verify/config.yaml`) closes
the loop without a human: after the pass, a draft with a contradicted or
unverified claim is revised through the same `revise_item` call with no
instructions (`verify/autorevise.py`, decision note `auto: fix fact-check
failures`), supported verdicts are carried over, the new claims are checked,
and so on until all are supported or `max_rounds` per run /
`max_rounds_per_draft` for life (0 = uncapped, the shipped value) is hit. A revision that keeps the claim set
unchanged is discarded. The queue badges each draft with its automatic round
count. A table's contradicted cells join the round too (blanked cells never do).

```bash
python run_verify.py             # pending drafts with unchecked claims
python run_verify.py --dry-run   # list, no calls
python run_verify.py --redo      # replace earlier verdicts
python run_verify.py --auto-revise     # then revise and re-check failed claims
python run_verify.py --no-auto-revise  # one run without the loop
```

## Publishing (step 3)

`run_publish.py` reads approved drafts through `publish/store.py:fetch_approved`
and posts them to X via tweepy (`publish/client.py`, the only module that
imports tweepy). Slots, daily cap, minimum gap between posts and the
breaking-news rules live in `publish/config.yaml`, along with its own
`timezone:` — the zone the slot hours are read in (behaviour, not display; keep
it equal to the root `timezone:`, see [Display time zone](#display-time-zone)). With `slots: []` (the
shipped value) there are no windows: every run posts the top candidate once
`min_gap_minutes` has passed and the daily cap is not reached. Posting is manual only:
a human runs `run_publish.py --live` or presses the panel's "Publish now"; the panel has no
automatic publisher and `run_ops.py run` refuses a configured step that carries `--live`.

Safety gates, all of which must hold before a single tweet is sent:

- `--live` is passed **and** `PUBLISH_ENABLED=1` is set. Anything else is a
  dry run that prints the plan and posts nothing.
- The draft is claimed in a `BEGIN IMMEDIATE` transaction (table `schedule`)
  before the API call, so two overlapping cron runs cannot post it twice and a
  draft is never retried after a failure.
- Every text is re-checked in code right before posting (<= 280 chars, or the
  draft's own limit for a long post). Failures are
  logged as refusals and go back to the approval queue; nothing is auto-fixed.
- Breaking items (`fda*` sources, `company_*` PRs whose title mentions an
  approval) may post outside slots but still respect the daily cap and gap.
- The human's order from the panel's approved page (`schedule.position`,
  `publish/store.py:set_order`) is honoured before breaking and score;
  `--draft ID` considers one approved draft only (the panel's "Publish now"
  runs `--live --now --draft ID`). Reopening a draft in the queue deletes its
  `schedule` row when nothing of it went live — never claimed, or claimed and
  failed or refused (`publish/store.py:forget`) — so a saved order never comes
  back with it; a posted or partial row is left alone, and a draft with a tweet
  to its name cannot be reopened at all.
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
keywords, source cadences), `publish/config.yaml` (slots) or
`draft/voice.md`, as each suggestion names.

## Operations (step 5)

`run_ops.py` runs the whole pipeline unattended under cron or a systemd timer,
detects when a source or stage has silently stopped, backs up the database, and
tells you when something needs attention. It never posts, never calls
Claude, and never edits content; it runs the other CLIs as subprocesses.

```bash
python run_ops.py run                 # lock; ingest -> score -> draft [-> publish -> feedback]
python run_ops.py run --only ingest   # a subset
python run_ops.py run --only studio   # the studio's own entry: no run lock, only its own
python run_ops.py run --dry-run       # print the argv per step, run and record nothing
python run_ops.py health [--json] [--alert]   # checks; exit 1 if anything is 'fail'
python run_ops.py backup [--keep N]   # verified SQLite online backup into backups/ (+ playbook)
python run_ops.py status              # last run per step, last health, row counts, backup age
python run_ops.py prune --days 90     # ops-owned tables only (pipeline_runs, health_checks, alerts_sent)
```

Settings live in `ops/config.yaml` (step order, per-step `timeout_seconds` where 0 means
no limit, as `verify` uses, per-step `lock:` names, the control panel's automatic runs
`auto_run_enabled` / `auto_run_times` / `auto_run_steps` / `auto_run_grace_minutes` /
`auto_run_backup_hours` (the first automatic run each day also takes a verified backup),
health thresholds and budget caps, backup dir/keep and `with_db`, the files beside the
database kept with each backup as `pipeline-<stamp>.<name>` (the studio's
`studio_playbook.md`), alert channels and cooldown). A plain `run` leaves out the
`manual` steps and the `skip_when_busy` one, the studio: its session runs an hour or more,
which would hold the whole run (and the run lock every fire takes) for as long, so it has
a schedule entry of its own, `run --only studio`, which takes only the studio's own lock
(a run of `skip_when_busy` steps alone never takes the run lock). The
shipped staleness limits (13h, and `source_stale_min_hours`) fit three runs a day;
tighten them if a scheduler runs every 30 minutes. The `publish` step is
a dry run and stays one: posting is manual only, so `run_ops.py run` refuses to start when
any step carries `--live` (post from the panel's approved page instead). Steps whose CLI has not merged yet are skipped with a warning.

Health checks: the Claude Code CLI (`cli`: fails when `claude_code.binary` from
`config.yaml` is not found on PATH; it does not test the login), sources (error /
never ran / stale), staleness of ingest, score
and draft, unscored backlog and pending-draft age, per-day scoring and drafting
budget, publish `partial`/`failed`/stuck claims, feedback snapshots, backup age,
DB size and disk free, and required env var names (never values; `required_env`
ships empty, since Claude needs no key). Alerts go to
the log always, and optionally to a webhook (`ALERT_WEBHOOK_URL`, works for
Slack/Discord/Mattermost incoming webhooks) or email (`SMTP_*`,
`ALERT_EMAIL_FROM/TO`). A check that keeps failing is re-sent only after
`alerts.cooldown_hours`; a recovery sends one message. Alerts carry check names,
summaries and counts only.

Deploy files: `deploy/crontab.example`, `deploy/pipeline.service`,
`deploy/pipeline.timer`, `deploy/pipeline-studio.service` and `deploy/pipeline-studio.timer`
(the studio's own entry, with no unit time limit: each stage has its own), and
`deploy/README.md` (VPS setup, lock/backup/log locations, how to restore a backup).

## Conference abstracts and KOL list (step 6)

Two more ingest sources plus retry in the HTTP layer. Nothing here calls
Claude or writes to X.

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
for. Nothing posts; Claude is called only where step 2 already
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
first-post length change below -40 chars means posts run long, a thread cut
in more than half of the edits means lead with the story, and a note
word such as "hype" or "jargon" recurring three times is a tone proposal. Edit
`voice.md` by hand; the next run picks it up.

Drafting model resolution: `DRAFT_MODEL` in `.env`, else `models.drafter` in
the root `config.yaml` if you add that key, else `model` in
`draft/config.yaml`. Databases created before step 7 are migrated in place
(`decisions.category` is added with a guarded `ALTER TABLE`).

## Swarm drafting (step 9)

Step 9 takes the human out of the creative loop. Instead of one strong model
writing a thread, many cheap calls each write ONE post ("cell") of it: a
genome names the slots (`hook`, `mechanism`, `thesis`, `catalyst`, `risk`,
`closer`) and each slot's one-line job; `fan_out` proposals per slot are
followed by `layers - 1` Mixture-of-Agents rounds where each cheap call sees
every earlier candidate and writes a better one; cells that fail the per-post
hard rules (280 chars, advice phrases, a number not verbatim in the source,
a link of any kind, the hook's preprint label) are dropped in code; near twins
are removed; a single-elimination tournament of pairwise cheap judges picks
the slot's post, which becomes context for the next slot. One assembly call
then turns the chosen cells into the step 2 JSON (visual, why_it_matters,
claims_to_verify) through the drafter's own schema check, hard rules, chart
check and retries. The bet ("more is different": the arrangement, not the
model, carries the quality) is measured, not assumed: with `control.enabled`
the single strong drafter also writes the story, a jury of `judge_votes`
cheap judges compares the two threads with the A/B order randomised, and the
winner is stored as the ordinary pending draft (`model` column `swarm:<model>`
for a swarm win). A swarm that fails its rules loses to the control. With
`jury: human` (shipped) no judge runs: both variants are stored as one draft in
status `choosing` (the other in `drafts.choice_json`) and the queue's `/choose`
page shows them blind as A and B; the pick becomes the pending draft
(`approval_queue/choosing.py`) and is recorded as the run's winner.

```bash
python run_draft.py               # swarm on (swarm/config.yaml enabled: true)
python run_draft.py --no-swarm    # the single strong drafter only, as before step 9
```

Settings live in `swarm/config.yaml` (cheap `model`, `assembler_model`,
`fan_out`, `layers`, `parallel_calls`, `judge_votes`, `jury`, `max_similarity`,
`control.enabled`). `parallel_calls` runs a layer's cells and a tournament round's
judge matches side by side; slots and layers stay in order, so it only changes the wall
clock.
Every call goes through `draft/drafter.py:call_anthropic`, one Claude Code CLI
run per call. Tables (step 9's
own): `swarm_genomes` (the heritable slots and topology; phase three writes
children), `swarm_runs` (genome, winner, call count and the full cell and
tournament log per story) and `swarm_variants` (both drafts of a run and
whether each passed the hard rules), so later phases can score the jury and
the genomes against real X engagement (`prompts/prompt9.md`). About 110 cheap
calls per story at the shipped values. The human's remaining creative-adjacent
controls are "Publish now" and "Set schedule" on the approved page.

**Phase two: fitness from X** (`run_evolve.py`, no network). Three seed
genomes (`default-6`, `wide-6` with more proposals and no synthesis layer,
`deep-4` with four slots and two synthesis layers) are drafted round-robin
(`swarm.store.next_genome`: the live genome with the fewest runs since the
newest one was born; designers and formats go to the one this writer has met
least). Once `run_feedback.py snapshot` has metrics, `run_evolve.py score`
gives every posted swarm draft the head tweet's KPI (`evolve.kpi`) read on its
first snapshot at least `horizon_hours` old (48; younger posts wait), without
the thread's own post-2 reply (`subtract_self_reply`), the median KPI of the
posts in the trailing `baseline_days` before it, and their smoothed ratio
`(value + smoothing) / (baseline + smoothing)` (a slow week prunes nobody),
stored in `swarm_fitness` (rows for runs not scored under the current rules are
dropped). Credit follows authorship: `swarm_fitness.genome_id` is NULL when the
control's text was posted or the format was a single post (its fan-out and
layers ran, but not its slot rules, which are what breeding mostly changes), and
`designer_id` unless `run_draft.py` drew a chart in that designer's Style
(`swarm_runs.styled`; a table, a failed render or no picture never used it). The
Style is recorded on the draft (`drafts.style_json`), so a later redraw (a
revision, the verifier's table) keeps it.
Which genome drafts the next story is drawn by Thompson sampling on those
credited scores (`evolve.allocation: thompson`, `swarm.store.thompson_next`): a
genome that has done better drafts more stories, one with little evidence still
gets some, and a child starts from its parent's record (its parent's own prior
included), a head start that halves every `dead_half_life` runs that can never
be credited to it (`dead_runs`: its swarm text not posted, its drafts never
posted within `dead_after_days`); a kind with no scored post yet rotates
evenly. A genome with no credited post after `max_dead_runs` such runs is
retired when the confidence rule retires nobody. `prune` (`prune_rule:
confidence`) retires a live genome with at least `min_posts` credited, scored
posts only when its mean log ratio is below the rest's with probability
`1 - (1 - retire_confidence) / k` (a t test, per look), at most `max_retire_per_run` per kind per run
and never below `min_alive` live genomes (`swarm_genomes.retired_at`,
`retired_reason`); `prune_rule: median` is the old below-the-median rule. `report` prints the per-genome table and the
swarm-vs-control measurement: median ratio of posts the jury gave to the swarm
against posts it gave to the control. `fetch_head_metrics` in `swarm/store.py`
is the one read of step 3's `posts` and step 4's `tweet_metrics`, empty when
either is missing. The `evolve` step in `ops/config.yaml` runs after `feedback`
on every scheduled run (both enabled: the account has the paid X read tier).

**Phase three: breeding and designers** (`run_evolve.py breed`, part of the
default run). After pruning, every gap under `evolve.population_size`
(`evolve.designer_population_size` for designers, six in the shipped config so
the pictures keep varying) is filled by a child of a top scorer. A **writer** child is written by ONE strong-model
call (`evolve.mutation_model`, blank = the drafting model; `swarm/mutate.py`,
through `draft/drafter.py:call_anthropic`) that reads the live genomes with
their scores and best posts and varies exactly one thing: reword a slot's rule,
split a slot, merge two, change `fan_out` or `layers`. Code checks that exactly
one thing changed, that the hook is first and the closer last, and that the
child stays within 3-6 slots, fan-out 2-12 and 1-4 layers; an invalid answer is
retried, then skipped. A genome owns its topology: `fan_out` and `layers` in
`swarm/config.yaml` are only the fallback for a row without them. **Designers**
are the picture side: a designer genome is a `draft/chart.py:Style` preset the
chart or table is first drawn with (the grader loop still adjusts from there):
layout knobs plus the card's `palette` (one of the named palettes in
`draft/chart.py:PALETTES`) and `multi_colour` (one hue per bar). Six seeds
(`house`, `compact`, `bold`, `teal`, `vivid`, `midnight`) are drawn round-robin
(`next_designer`, recorded in `swarm_runs.designer_id`), scored with the same
relative KPI, pruned the same way, and bred without a model by stepping one
knob at random inside its range, flipping a flag or swapping the palette
(colour moves are drawn as often as every layout knob together). Children carry `parent_id`, so the family tree
is readable on the panel's **/swarm** page (`ops/store.py:fetch_swarm_population`
and `fetch_swarm_bet`, read-only; the page breeds and retires nothing). Until a
genome has `min_posts` scored posts (`format_min_posts` for a format), nothing
of its kind is bred unless `--force`. Because selection acts on the
topology, the population can end up somewhere nobody designed, a single wide
layer included, if that is what X rewards.

**Phase four: the format is a gene.** Phases one to three evolved inside three
fixed opinions: every draft a 3-6 post thread, exactly one picture, on the
first post. Phase four makes them a third population, **format genomes**
(`swarm/genome.py:SEED_FORMATS`): `thread-1-first` (the old physics),
`thread-2-ends` (a chart on the first and the last post), `thread-0` (no
picture), `single-1` (one 280-character post) and `long-1` (one Premium
long-form post of up to `formats.long_max_chars`, shipped 4000; X allows 25000
on Premium and the API rejects a long post from a non-Premium account). They
are drafted round-robin like writers and designers, and the swarm and the
control write the same story to the same format so the jury compares like
with like. `draft/schema.py:Format` is what the drafter, `validate_output` and
`check_hard_rules` read; without one they require the old physics, so nothing
changes for a draft made before phase four. The first picture may be a chart
or a table (step 2b's table checks are unchanged); a second must be a chart
(`visuals` in the output, numbers verbatim). Each picture is rendered to its
own file, recorded in `drafts.images_json` with the post it is anchored to,
shown on the queue's draft page, and attached by `run_publish.py` to that
post; `publish/thread.py` checks each post against the draft's own limit, so
a long post is never refused for being over 280. The swarm runs one cell for
a single post and its slots as sections of `formats.long_section_chars` for
a long one. A single or long format is exactly one post: no post of any shape
carries a link, so there is nothing to put in a second one, and
`publish/thread.py` leaves it unnumbered. Formats are scored with the same relative KPI, pruned only after
`evolve.format_min_posts` posts (a coarse gene needs more evidence than a slot
rule), and bred without a model by stepping one field (shape, picture count,
an anchor, the post range) to a neighbour. The panel's `/swarm` page has a
Formats table.

## The studio (step 10)

The studio makes the account's main posts the way its best ones were first made by
hand: ONE long Claude Code session per piece on Opus 5.5 at max effort
(`studio/config.yaml` `model`, `effort`), which researches, writes, designs and
fact-checks the whole thing itself. The app decides what to ask for, checks what
comes back and puts it in the approval queue; it never edits the writing.

**Stages** (`studio/session.py`; each one is a CLI run on the same session, the
first with `--session-id`, every later one with `--resume`, so the session keeps
everything it read):

1. *Research*: the topic (typed by the editor, a feed story, or chosen by the
   session from the top scored stories of the last `topics.lookback_hours` that the
   account has not written about, no studio piece or queued topic on them and no draft
   that did not fail, or from its own news scan) becomes `factbase.md` (every fact with
   its URL, opened or snippet, knowledge marked, verified X handles, corrections,
   open questions) and `research.json` (topic, why now, companies, candidate
   angles, and the story it used: kept only when it is one the brief offered,
   recorded as `offered_stories` in the piece's meta). A piece started by hand stops here (`research_ready`) until the editor
   presses Continue with optional notes; automatic pieces write straight through.
2. *Write*: the session picks the angle from those on offer, the shape
   (`long_post`, `thread` of long posts, or `short_post`) and the hook, writes
   `posts/NN.txt`, designs `cards/card_N.html`, runs a cold fact-check with a
   fresh sub-agent (Agent tool) that sees only the post and card text, waits for its
   report (the CLI runs with background tasks off, so the agent always runs in the
   foreground), logs every finding in `factcheck.md`, and writes `piece.json`. A piece
   without that log is blocked: it goes back to the session, and never to the queue.
3. *Polish*: the app draws every card (`studio/render.py`: headless Edge, Chrome or
   Chromium with the network blocked, a content policy that runs none of the card's own
   scripts and loads nothing from the web or the disk, and the house fonts injected; a
   layout check reports text cut off, overlapping or off the card and any empty band
   taller than a quarter of the card; the window is grown by whatever the browser keeps
   for itself and the picture cut back to the card, so the footer is never lost), counts
   characters the way X
   does (`studio/xcount.py`), runs the safety lines (`studio/safety.py`:
   investment or medical advice and a price target, fair value or value per share
   of the account's own, in the posts and on the cards and their alt text; links
   including bare domains, in the posts) and checks that every @handle has a page
   that verified it, and that a post or card citing an analyst's target
   (`draft/targets.py` finds them) has entries in piece.json's `price_targets`: the
   firm, the date, what it rests on, the catalysts the post says to watch, whether
   they are in the model and, if not, what they would move, a source, and
   `post_says`, the post's own words on what the target rests on, which must be in a
   post or card (`studio/qa.py:check_price_targets`; a figure given as a target that
   no entry lists as its target, the one before or a published case goes back too).
   Whether those words hold up is for the cold fact-check and the editor. Problems go back to
   the same session for up to `max_polish_rounds`; at least one round always shows
   the session its rendered cards. A piece that still has a blocking problem ends
   `failed`; one with only fixable leftovers goes to the queue with them listed.
4. *Queue*: `studio/ingest.py` inserts a pending draft (`item_id` `studio:<id>`,
   shape `long` at the studio's `x.long_post_max`, no claims to verify so step 2b
   leaves it alone, every card copied to `<db folder>/images/` and anchored to its
   post, each `recheck_before_posting` fact a `Re-check before posting:` line of
   `why_it_matters` that the copy-paste posting page lists, plus one naming the
   analyst targets the posts still cite). Publish chains a thread
   of long posts as replies, unnumbered. Each ingest records what it put in the
   queue (`queued` in the piece's meta) as soon as the text is in, with the cards
   attached so far: a card that cannot be copied (a full disk, a picture another
   program holds open) fails the piece without its own revision ever reading as the
   editor's hand edit, and Resume polishes again and attaches the rest.
5. *Revise*: the studio page's Revise resumes the session with the editor's notes,
   re-checks and replaces the queue draft: a pending one, or a rejected one that
   comes back to pending (never one live on X; its old publishing order is
   forgotten, as the queue's Reopen does). The editor's own changes in the queue
   since the last ingest (`studio/ingest.py:hand_edits`: text edited by hand, cards
   dropped) are written into the session's files first and named in the revise
   prompt, and the queue door refuses to replace a draft whose changes the session
   never saw, so a hand edit is never reverted. While the studio works on a piece,
   or a run of it waits, the queue holds its draft (`approval_queue/store.py:studio_hold`:
   approve, edit, reject and the picture drops answer 409). The queue's own Revise
   refuses a studio draft and links to the studio page. Studio drafts never feed the
   drafter's voice examples or voice report, and the weekly report's feed and
   voice-guide proposals leave the `studio` group out.

**Variety**: `studio/angles.yaml` holds 19 angles (deal decoder, class deep dive,
catalyst map, readout reaction and preview, the race, head to head, post-mortem,
regulatory decoder, follow the money, patent cliff, origin story, mechanism for
investors, contrarian take, bull vs bear, one chart, conference playbook, weekly
watchlist, scorecard). The angles of the last `variety.avoid_recent_angles` pieces
are not offered, and the session is told the recent hook styles, shapes and
opening lines to avoid. The research prompt lists every piece started in the last
`topics.avoid_days` days that was not discarded, finished or not (one waiting at the
checkpoint or stopped says so), as topics not to repeat. **One story, one piece of
writing**: `run_draft.py` skips a story the studio holds (a piece on it that was not
discarded, at any stage, a topic queued for it, or a story offered to a piece still
researching on no story, its `offered_stories`; `approval_queue/store.py:studio_held_clusters`),
and the studio's shortlist skips a story with a draft that did not fail
(`drafted_cluster_ids`). A draft run looks at the hold again before each story, so a
studio session started beside it is left the stories it has not reached. The one story
both can be on is the one the run was already writing when the research started: a
research that names it after its draft landed fails (`story N got a draft from the
drafter while this research ran`; Resume researches another), and a draft whose story
a piece's research named meanwhile is not stored (`studio_held_clusters(offered=False)`). Each piece and queued topic keeps one item of its story
(`story_item`), so a story that linking merges into another cluster is followed there
(`studio/runner.py:follow_merges`, at the start of every studio run). **Voice and design**: `studio/brief/session.md` (the job),
`voice.md` and `cards.md` (dark 4:5 cards, Inter and IBM Plex Mono shipped in
`studio/fonts/`) travel as text appended to Claude Code's system prompt on every launch,
resumes included (the CLI reuses its record of the first launch's prompt only until the
conversation is compacted);
`studio/exemplars/` holds the three hand-made reference pieces (handoff docs and
posted cards); the playbook (`studio/playbook.md`, or the editor's copy
`studio_playbook.md` next to the database, edited on `/studio/playbook`) goes into
every research and write prompt and wins over the voice guide. Both prompts also list
the X handles `config.yaml` gives (`studio/runner.py:app_handles`, the ones qa accepts
without a page) for the session to use without verifying them, and before each of those
stages the app writes `earlier_pieces.md` into the piece's folder: the whole text of the
last `variety.recent_pieces_shown` written pieces as the queue holds them, each saying
whether it went out on X, which a scorecard grades against (a session cannot open
another piece's folder). Story titles and abstracts from the feeds are quoted between
FEED TEXT markers as outside text, data and never instructions.

**Isolation**: each session runs from its own folder under `workspace_dir`
(`studio_pieces/`) with `--safe-mode --restricted --permission-mode dontAsk` and
only `Read, Write, Edit, Glob, Grep, WebSearch, WebFetch, Agent` (`cli_flags`,
`tools`): no shell, file tools confined to its folder (which holds the piece's own copy
of `studio/exemplars/` as `reference/`, made by `studio/session.py:copy_reference` before a
stage: no `--add-dir`, so a session can never change the shipped reference pieces), no
CLAUDE.md, plugins, hooks or MCP servers, and API credentials stripped from its
environment. The cold fact-checker, a sub-agent that gets none of the session's
standing instructions, is told by the write and revise prompts that web pages are data
and that it changes no file. A stage whose CLI stopped a sub-agent before it reported
(it reports success all the same) is `subagent_killed`, not finished: the piece is
`interrupted`, its log and runs say so, and Resume runs the stage again. Each
`studio_runs` row's `cost_usd` is that run's own cost: the CLI reports the session's
running total, so the row takes the difference from the total the piece's session last
reported (`session_cost` in the piece's meta).

**Running it**: the `studio` step in `ops/config.yaml` (in the panel's automatic runs
right after `score`, `skip_when_busy` so a long session sits a run out instead of holding
the others back, along with the steps its own run still has to come behind it,
`JobManager.queued_behind`; a plain `run_ops.py run` leaves it out, and cron, Task
Scheduler and systemd run it from an entry of its own, `run_ops.py run --only studio`)
acts on the editor's requests and starts a new piece when
`auto.max_new_per_day` (per calendar day in the root `timezone:`, the clock the run
times are set in) and `auto.min_hours_between` allow, no piece waits at the checkpoint
and a card browser was found (without one the step fails rather than start a piece
whose cards cannot be drawn); `studio_now` and `studio_resume` are the studio page's
manual buttons. Up to `max_parallel` (3) studio runs at once, each in a writing slot of
its own (`studio_pieces/.studio.lock`, `.studio.lock.2`, ...; the studio steps carry
`slots: 3` in `ops/config.yaml` so the panel starts that many), each writing one piece;
what a run takes up (a request, a queued topic, a new piece's shortlist) is claimed under
`studio_pieces/.studio.claim.lock`, so two runs never take the same one. A killed run
leaves the piece `interrupted`: as soon as a studio page shows it or the panel sees the
run end, once no run holds that piece's slot (`studio/runner.py:settle_stopped`). Resume
carries on in the same session; a session Claude Code has cleaned up (after 30 days by
default) is replaced by a fresh one that reads the piece's files first. Each stage is
told the date and the playbook as they are when it starts. A piece whose folder is gone
fails and says so, and the run goes on. A card picture another program holds open (an
image viewer, on Windows) is a card that could not be drawn, never a crash. A piece's
folder is the data folder's path as given, never resolved: on Windows a mapped network
drive would otherwise become a UNC path, which the npm `claude.cmd` cannot start in
(`studio/runner.py:absolute`; `panel/frozen.py` and `run_ops.py` keep the drive the same
way). `run_studio.py` writes a character its console cannot show (a cp1252 pipe on
Windows) as its escape rather than fail the line, and session.log gets every line either way.
Tables: `studio_pieces`, `studio_runs`, `studio_topics`, `studio_scans`,
`studio_radar_topics`, `studio_catalysts`, `studio_playbook_versions`,
`studio_manual_metrics`.

**The radar** (`studio/radar.py`, pure; `studio/scan.py`; `/studio/radar`;
`studio/config.yaml` `radar:`) is where topics come from. Once a day (`run_studio.py
--scan`, the automatic `studio_scan` step right before `studio`, its own lock; at most every
`scan_every_hours`) one call on the writer's model and effort, through
`studio/scan.py:call_scanner` (`claude_cli.run_claude` with WebSearch and WebFetch only and
`radar.timeout_minutes`), is given the angle library, the account's recent pieces, the
feed's top stories and the catalysts already on the calendar, and answers in JSON: 3 to
`max_topics` topics (title, why now, a suggested angle key, companies with tickers, the
sources it opened) and the dated catalysts coming up (date as precise as the source: a
day, a month, a quarter or a half; company, ticker, drug, kind, detail, source).
`radar.parse_scan` keeps what checks out (an unknown angle becomes blank, sources must be
http(s), a catalyst needs a company and a date between `past_days` back and
`calendar_days` ahead); an unusable answer or a failed call marks the scan failed and stores
nothing. Every piece's research adds the catalysts it confirmed (`research.json`
`catalysts`, through `studio/scan.py:harvest`) and names the radar topic it took
(`radar_topic`). Catalysts merge by company or ticker, kind, drug and start date. An open
piece's research prompt lists the recent scans' untaken topics, the catalysts in the next
`brief_upcoming_days` and the last `brief_recent_days`, and the feed's stories. The page
shows them with **Write it** (a topic queued with the scan's reasons and sources and the
selected angle), **Write the preview** / **Write the reaction** (a catalyst, with
`radar.PREVIEW_ANGLES` / `REACTION_ANGLES` selected) and **Dismiss**, plus **Scan now**
(the manual `studio_scan_now` step). Claiming a queued topic marks its radar topic or
catalyst with the piece; dropping it puts them back.

**Learning from X** (`studio/learn.py`, pure; `studio/evidence.py`, the reads;
`studio/config.yaml` `learn:`): each posted piece is scored on its first post at
`horizon_hours` (48) after posting, on the conversation KPI
(`feedback/models.py:CONVERSATION_WEIGHTS`), from step 4's snapshot or from numbers the
editor typed in, as (value + smoothing) / (median of every head the account posted in
the `baseline_days` before it + smoothing); a piece with fewer than
`min_baseline_posts` before it is measured but not scored. Per angle, shape, hook style
and card count the page and the prompts show the scored pieces and their mean
log-relative as "x the median". Every brief then carries a WHAT X SAYS section (the
counts, the best and worst values and openings, a small-sample caveat under twenty
pieces) and a **lean**: one Thompson draw per arm (a normal posterior on the log scale,
prior at the median, `prior_sd`, `post_sd` until ten pieces measure the spread) among the
angles, shapes and hooks the variety rules leave on offer, seeded by the piece id and kept
in its meta, so a better value is suggested more often and an untried one still gets
tried. The **playbook rewrite** (`studio/playbook.py:rewrite`, the loop's one model call,
`call_rewriter` through `claude_cli.run_claude` with no tools, on `learn.model`/`effort`,
blank = the writer's) runs once `rewrite_min_new` scored pieces are new to the last
rewrite and `rewrite_min_hours` have passed; it gets the current playbook, the evidence,
every measured piece and the editor's hand edits of studio drafts, and must answer with
JSON whose playbook keeps the required sections and `max_words`. `learn.playbook:
propose` (shipped) keeps it for the editor to apply, since the numbers mean little before
20 to 30 pieces; `auto` applies it, `off` never rewrites; a failed call or a rejected reply
changes nothing. Every playbook ever in use is a row of
`studio_playbook_versions` (seed, editor, learned, proposal, revert, with the changelog,
the evidence and the pieces it learned from) and the file sessions read is replaced
atomically. `run_studio.py --learn` (the automatic `studio_learn` step after `feedback`,
its own lock) measures and rewrites when due, `--learn-now` (the manual
`studio_learn_now` step) rewrites now, `--learn --dry-run` prints the evidence and the
prompt. `/studio/performance` shows the posted pieces, the per-arm table with how often
the lean suggests each value, the learning state and the playbook history (diff,
changelog, apply, put back), with forms for typed-in numbers and for the link of a post
confirmed by hand without one (`publish/store.py:set_head_tweet`, through
`panel/publishing.py:add_head_link`, replaces only post 1's `manual-` marker).

## Claude Code CLI

Every model call the pipeline makes runs the Claude Code CLI in print mode,
logged in with your own account (`claude login`, see [Setup](#setup)): the
scorer, the story linker, the `--auto-rate` rater, the drafter (revisions, the
swarm's cells and its breeding included), the image grader and the claim
verifier. There is no API key and no other backend, so every call counts
against that account's usage. The CLI's settings live in the root `config.yaml`:

```yaml
claude_code:
  binary: claude           # on PATH, or its full path if a scheduler's PATH lacks it (cron's usually does)
  timeout_seconds: 600     # per call; verify/config.yaml sets its own for a claim check
  extra_args: []           # appended verbatim, e.g. ["--fallback-model", "sonnet"]
  safe_mode: true          # --safe-mode on every call; false only for a CLI too old for it
```

`.env` holds nothing for Claude. An `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN`
in `.env` or the environment is ignored: `claude_cli.cli_env` strips both from the
CLI's environment, so a stale key can never switch it to metered API billing. Background
tasks are switched off there too (`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`, and a
`CLAUDE_AUTO_BACKGROUND_TASKS` in your environment is dropped): the app waits for each
call, and an agent the CLI moved to the background would be killed unfinished when the
call ended, as a studio session's cold fact-check once was. An
`LLM_BACKEND` line left in an older `.env` does nothing either. `run_ops.py health`
and the dashboard carry a `cli` check that fails when `claude_code.binary` is not
found on PATH; it does not test the login, which a step's log reports as
`Not logged in` (run `claude login` again).

How it works: `claude_cli.run_claude` is the only place that spawns the CLI. It
writes the system prompt to a temp file (deleted after the call) and runs the argv
`claude_cli.build_argv` builds,

```
claude -p --output-format json --verbose --no-session-persistence \
  --tools <list> --model <model> [--allowedTools <list>] \
  [--system-prompt-file <file>] [--effort <level>] [--safe-mode] [extra_args...]
```

with the user prompt on stdin. The tool list is empty for every caller but two, and
whatever it names is pre-approved with `--allowedTools`, since print mode cannot
answer a permission prompt: the claim verifier (`WebSearch,WebFetch`) and the image
grader (`Read`, to open the PNG). The system prompt travels in a file because it is
long and full of quotes, which a Windows `.cmd` wrapper cannot pass safely. Each call
runs from a new, empty folder of its own under the temp directory, readable by this
user only and removed afterwards (`claude_cli.private_workdir`, which also holds the
system prompt's file): not the repo, so this project's `CLAUDE.md` never reaches the
prompt, and not the shared temp folder, where any account on the machine could leave a
`CLAUDE.md` or a `.claude/settings.json` with hooks. `--safe-mode`
(`claude_code.safe_mode`, on by default) keeps the operator's own Claude Code set-up
out of every call while the login still works: their `CLAUDE.md` files (including the
folders above the call's), hooks (a Stop hook would run on each of the hundred-odd calls
of a swarm draft), MCP servers, plugins and output styles, any of which could reword a
reply that must be JSON. `--bare` is not used because it would also skip the stored
login.
`--verbose` returns the whole transcript, so when the final turn comes back empty the
last assistant text (or the input of the last tool call it made) is recovered
(`claude_cli.parse_envelope`). On Windows the npm `claude.cmd` wrapper is resolved to
its full path, the child opens no console window, and a timeout kills its whole
process tree.

Print mode has no tool calling, so the scorer puts the scoring tool's strict JSON
schema (`score/rubric.py:TOOL`) into the system prompt and checks the reply in code:
a reply that is not a JSON object with a `scores` list fails the batch (logged,
skipped, picked up next run); a CLI failure (a usage limit, an error inside the CLI,
a timeout) is retried with exponential backoff (`scoring.max_retries`,
`scoring.backoff_seconds`); a CLI that cannot be found or started stops the scoring
run. Score rows keep the CLI's verbatim reply in `raw_response`
(`{"backend": "claude_code", "model": ..., "text": ...}`). A safeguard verdict
(`safeguards flagged this message`) is `ClaudeCliRefused`: it is deterministic for a
given prompt, so it is never retried; instead `Scorer.score_batch_splitting` halves the
batch and scores each half in its own call, down to single clusters, and a cluster
refused on its own is logged and left unscored. The drafter treats the same two errors
the same way: a refusal is never resent (`draft/drafter.py:generate` raises it at once,
and `run_draft.py` stores the story `failed` with `refused by the usage-policy
safeguard`, so `--retry-failed` is the only way it is tried again), and a CLI that
cannot start ends the drafting run. A call that fails for any other reason (a usage
limit, a timeout) after an attempt broke a hard rule leaves the story for the next run
rather than storing it `failed` with that attempt's reasons. `models.scorer_effort`,
`models.drafter_effort`, `models.rater_effort` and `verify/config.yaml:effort` set the
effort level per phase (the CLI's `--effort`; blank means the model's default).

Trade-offs of running everything through the CLI:

- The account's rolling usage limits are shared with your own Claude Code
  sessions; a limit hit stalls scoring and drafting until it resets.
- The machine that runs the pipeline (the panel, cron or Task Scheduler) needs
  Claude Code installed and kept logged in, as the user the steps run as.
- Reply shape is requested, not enforced; expect an occasional skipped batch.
- Anthropic's terms treat consumer subscriptions as covering their own products,
  not programmatic use. A scheduled pipeline sits in a grey area; check the terms
  of the plan the CLI is logged in with.

## Database

SQLite (`db_path` in config). Tables: `items`, `clusters`, `scores`,
`ratings`, `source_runs` (step 1); `drafts` (with `chart_json` and
`image_path` for the rendered chart in `<db folder>/images/`), `decisions`,
`draft_examples` (steps 2 and 7); `schedule`, `posts` (step 3); `tweet_metrics`,
`follower_snapshots`, `feedback_reports` (step 4); `pipeline_runs`,
`health_checks`, `alerts_sent` (step 5). Every score is kept, so re-scoring
after a prompt change is additive. Each step creates only its own tables and
reads the others through adapter functions in its `store.py`.

## Display time zone

Storage never changes: every timestamp in SQLite is an aware-UTC ISO string, and
every comparison, window and API payload is UTC. Conversion happens only when a
datetime becomes text for a person.

`timeutil.py` is the single place that does it. `timezone_name()` reads
`timezone:` from the root `config.yaml` (shipped value `America/Los_Angeles`,
Seattle; it is the only reader of that key, cached, falling back to the default
when the name is unknown), `display_tz()` returns the tzinfo, `to_display()`
takes a datetime or a stored ISO string and returns it in that zone,
`fmt_datetime()` renders `2026-06-01 08:30 PDT`, `fmt_date()` renders the
calendar date, and `install_jinja_filters(env)` registers the `|localtime` and
`|localdate` filters used by the panel and approval-queue templates.

Converted: the panel's dashboard, publishing and feedback pages, the approval
queue's draft detail and voice pages, `panel/feed.py`, `digest.py`,
`run_ops.py status`, the `ops/health.py` report heading, the `ops/alert.py`
alert body, the `feedback/report.py` heading, and the window line and edit
headings in `draft/voice_report.py`.

Deliberately still UTC, because they are sort/parse keys rather than something
to read: the backup filenames `backups/pipeline-<UTC stamp>.sqlite`, the
`run_id` stamps, and the `captured_on` day bucket from
`feedback/store.py:day_of`. Relative ages ("3.2h ago") are zone-independent.

`publish/config.yaml` and `feedback/config.yaml` keep separate `timezone:` keys
because they drive behaviour — the posting slots and the report's "hour posted"
column — not display. All three ship as `America/Los_Angeles` and should be
changed together.

## Known source caveats

- EurekAlert no longer publishes RSS; bioRxiv/medRxiv RSS was retired, so
  those use the `api.biorxiv.org` JSON API.
- `fda.gov` (OCE approvals page) and `clinicaltrials.gov` sit behind bot
  protection that blocks some cloud egress. Both sources fail soft; run from a
  residential/VPS IP or add a proxy if they return 401/403.
- Several big-pharma newsrooms have no public feed or block bots; the company
  list in `config.yaml` contains only feeds verified to work.

