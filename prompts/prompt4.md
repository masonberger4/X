You are building step 4 of the pipeline described in PLAN.md: the feedback
loop. It pulls impressions, engagement and follower counts for posts that
step 3 published, stores them as time-series snapshots, and turns them into a
weekly report that tells a human which sources, formats and topics perform,
plus concrete suggestions for tuning the scoring rubric and prefilter. Read
PLAN.md and CLAUDE.md first.

Another session is building step 3 (publish layer, branch
claude/step3-publish, package publish/, CLI run_publish.py) IN PARALLEL. Steps
1 and 2 are already merged on main. To avoid merge conflicts, follow these
boundaries strictly:
- Work on branch claude/step4-feedback.
- Do NOT create or edit db.py, config.py, config.yaml, ingest/, filter/,
  score/, run_ingest.py, run_score.py, or digest.py (step 1); draft/,
  approval_queue/, run_draft.py, or run_queue.py (step 2); publish/ or
  run_publish.py (step 3, in progress).
- Do NOT add tweepy or any other dependency. Everything you need is already
  in pyproject.toml (httpx, pyyaml, python-dotenv, pydantic). The only
  pyproject.toml change allowed is adding "run_feedback" to
  [tool.setuptools] py-modules and "feedback*" to packages.find include.
  Ask before adding anything else.
- Put your own storage in feedback/store.py, creating your own tables with
  CREATE TABLE IF NOT EXISTS in the same SQLite file. Resolve the path the
  same way approval_queue/store.py:db_path does: DB_PATH env var, else
  config.yaml db_path, else ./pipeline.db. Never modify tables you did not
  create.
- Never auto-edit config.yaml, score/rubric.py, filter/prefilter.py or
  draft/voice.md. The feedback loop PROPOSES changes in a report; a human
  applies them. The rubric's PROMPT_VERSION bump and re-score are the
  human's call.

Read other steps' tables only through adapter functions in
feedback/store.py, each with the assumed schema in a comment so it is easy
to reconcile when branches merge:

  fetch_posted(since) -> list[PostedTweet]
    Reads step 3's posts table. Step 3 has NOT merged yet; assume the schema
    its kickoff prompt (prompts/prompt3.md) specified:
      posts(id INTEGER PK, draft_id INTEGER, tweet_id TEXT, text TEXT,
            kind TEXT,         -- 'single'|'thread'
            position INTEGER,  -- 0-based within a thread
            posted_at TEXT, slot TEXT, error TEXT)
    Only rows with tweet_id NOT NULL and error IS NULL are metric-eligible.
    Treat the position-0 tweet of a thread as the head; report thread
    metrics as the head's metrics plus the sum of reply-tweet metrics,
    shown separately.

  fetch_post_context(draft_ids) -> dict[draft_id, PostContext]
    Joins the REAL step 1 and step 2 tables as they exist on main:
      drafts(id, item_id TEXT, cluster_id INTEGER, model, single_post,
             thread_json, suggested_visual, why_it_matters, claims_json,
             status, rejection_reason, snoozed_until, created_at, updated_at)
      decisions(id, draft_id, action, original_text, edited_text, note,
                created_at)
      items(id TEXT, source, url, doi, title, abstract, published_at,
            fetched_at, dedup_hash, cluster_id, raw_json)
      clusters(id, title, norm_title, doi, published_at, created_at,
               prefilter_status, prefilter_reason)
      scores(id, cluster_id, model, prompt_version, novelty,
             clinical_significance, audience_interest, expertise_fit,
             timeliness, evidence_level, hype_risk, total, rationale,
             suggested_angle, ...)   -- use the LATEST row per cluster_id
      ratings(id, cluster_id, rating 1-5, note, rated_at)
    Note drafts has no source/url columns: source and url come from items
    via drafts.item_id. A draft "was edited" if any decisions row for it has
    edited_text that differs from original_text.

Build this structure:

  feedback/
    __init__.py
    client.py         # The ONLY place that talks to the X API, via httpx
                      #   (not ingest/http.py, which cannot send auth
                      #   headers). App-only auth: X_BEARER_TOKEN from .env.
                      #   Two functions:
                      #   get_tweet_metrics(tweet_ids) -> dict[tweet_id,
                      #     TweetMetrics]  # GET /2/tweets?ids=...&
                      #     tweet.fields=public_metrics,created_at ; batch
                      #     <= 100 ids per call; impressions, likes, reposts,
                      #     replies, quotes, bookmarks
                      #   get_user_metrics(username) -> UserMetrics
                      #     # GET /2/users/by/username/:u?user.fields=
                      #     public_metrics ; followers, following, tweets
                      #   Retry/backoff on 429 (honour x-rate-limit-reset,
                      #   capped by a max wait from config) and 5xx; never
                      #   retry on 401/403. Tweets missing from the
                      #   response (deleted) are returned with deleted=True,
                      #   not raised. Never log the bearer token.
    config.yaml       # your own file, NOT the root config.yaml: username,
                      #   snapshot schedule (daily for the first N days
                      #   after posting, weekly after, stop after M days),
                      #   report window, rate-limit max wait, the metric
                      #   used as the primary KPI (impressions), minimum
                      #   posts per group before a group-level conclusion
                      #   is reported. Load with pyyaml.
    store.py          # tables you own:
                      #   tweet_metrics(id PK, tweet_id, draft_id,
                      #     captured_on TEXT,  -- YYYY-MM-DD, UNIQUE with
                      #                        -- tweet_id: one snapshot/day
                      #     captured_at, impressions, likes, reposts,
                      #     replies, quotes, bookmarks, deleted INTEGER)
                      #   follower_snapshots(id PK, captured_on UNIQUE,
                      #     captured_at, followers, following, tweet_count)
                      #   feedback_reports(id PK, window_start, window_end,
                      #     generated_at, report_md, suggestions_json)
                      #   + the two adapters above + due_for_snapshot(now)
    analysis.py       # pure functions over dataclasses, no DB, no network:
                      #   latest snapshot per tweet; per-post rows (source,
                      #   evidence_level, kind, slot, hour posted, edited?,
                      #   score dimensions, human rating, metrics); group
                      #   summaries (median + mean impressions, engagement
                      #   rate = (likes+reposts+replies+quotes)/impressions)
                      #   by source, by evidence_level, by kind, by slot, by
                      #   edited-vs-unedited; rank correlation (Spearman,
                      #   implemented in pure Python, no scipy) between each
                      #   score dimension / total / human rating and the
                      #   KPI; follower delta over the window; top and
                      #   bottom 5 posts with their suggested_angle.
    suggest.py        # pure: turns analysis output into a list of
                      #   Suggestion(kind, target, rationale, evidence)
                      #   where kind is one of 'rubric_weight',
                      #   'prefilter_keyword', 'source_cadence',
                      #   'format', 'slot'. Only emit a suggestion when
                      #   the group has >= min_posts and the effect is
                      #   clear (e.g. median KPI differs by > 2x). Every
                      #   suggestion says what a human would change and
                      #   where (file + key) but changes nothing.
    report.py         # renders analysis + suggestions to markdown: a
                      #   headline block (posts, impressions, followers,
                      #   delta), group tables, correlation table, top/
                      #   bottom posts, suggestions, and a fixed caveats
                      #   section (public_metrics only; the Original
                      #   Content Rewards "Premium impressions" figure is
                      #   not exposed by the API, so impression_count is a
                      #   proxy; small-n warnings).
  run_feedback.py     # CLI with subcommands:
                      #   snapshot   pull metrics for every tweet that
                      #              due_for_snapshot says is due, plus one
                      #              follower snapshot per day; idempotent
                      #              (rerunning the same day pulls nothing).
                      #              --all ignores the schedule; --dry-run
                      #              (default OFF here, reads are safe)
                      #              prints the ids it would fetch.
                      #   report     --weeks N (default 1) --out FILE
                      #              renders the report from stored
                      #              snapshots ONLY, no network; also stores
                      #              it in feedback_reports.
                      #   followers  print the follower time series.
                      #   Designed for cron: snapshot daily, report weekly.
  tests/test_feedback_*.py

Requirements:
- Network only in feedback/client.py; tests monkeypatch
  get_tweet_metrics/get_user_metrics and never hit the network. Parse-level
  tests use a saved JSON fixture shaped like the real /2/tweets response
  (include an "errors" entry for a deleted tweet id).
- Snapshot schedule is data-driven from feedback/config.yaml; test it with a
  frozen clock: day 0..N daily, then weekly, then never after M days; a
  second run on the same day fetches nothing.
- Deleted tweets are recorded once with deleted=1 and never fetched again.
- All analysis is pure and unit-tested with hand-built rows: group
  summaries, engagement rate with zero impressions (no ZeroDivisionError),
  Spearman against a known answer including ties, follower delta with a
  single snapshot (delta = None, not 0), thread aggregation.
- Suggestions are conservative and tested: no suggestion below min_posts;
  one is produced for a clear-cut synthetic case; the report renders the
  "no suggestions" state cleanly.
- Report tests run end to end against a temp SQLite file: create step 1
  tables with db.py's init, step 2 tables with approval_queue.store's init,
  the assumed step 3 posts table by hand in the fixture, and your tables;
  seed fake items/clusters/scores/ratings/drafts/decisions/posts rows; run
  snapshot with a monkeypatched client, then report; assert the markdown
  contains the expected group rows and that feedback_reports has one row.
- Read secrets from .env via python-dotenv. Logging via stdlib: INFO for
  counts (tweets due, fetched, deleted, followers), WARNING for rate-limit
  waits and small-n groups, ERROR for API failures. Never log the token.
- Generated report text must never contain medical advice; it summarises
  metrics, not science.
- ruff check, ruff format --check, and pytest must pass before every commit.
- Add a "Feedback loop" section to README.md (the run commands, the cron
  suggestion, the public_metrics caveat, and where the human applies rubric
  changes). Do not otherwise rewrite README.md; append a section so it
  merges cleanly next to step 3's README section.

Start by proposing the three tables, feedback/config.yaml, and the
PostRow/Suggestion dataclasses, then implement analysis.py and suggest.py
with tests (they are pure and need no DB), then store.py with the
schedule, then client.py, then report.py and run_feedback.py. Commit as you
go and push to your branch.

Do not post, delete or edit anything on X; this step is read-only against
the X API. Do not touch the approval queue UI or the publish layer.
