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
`posts` (step 3); `tweet_metrics`, `follower_snapshots`, `feedback_reports`
(step 4). Every score is kept, so re-scoring after a prompt
change is additive.

## Known source caveats

- EurekAlert no longer publishes RSS; bioRxiv/medRxiv RSS was retired, so
  those use the `api.biorxiv.org` JSON API.
- `fda.gov` (OCE approvals page) and `clinicaltrials.gov` sit behind bot
  protection that blocks some cloud egress. Both sources fail soft; run from a
  residential/VPS IP or add a proxy if they return 401/403.
- Several big-pharma newsrooms have no public feed or block bots; the company
  list in `config.yaml` contains only feeds verified to work.

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
