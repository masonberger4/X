You are building step 3 of the pipeline described in PLAN.md: the publish
layer (X API v2 via tweepy), the posting scheduler, and the publish log.
Read PLAN.md and CLAUDE.md first.

Two other sessions are working IN PARALLEL on other branches: step 1
(ingest + score, branch claude/cancer-news-pipeline-step1-*) and step 2
(draft + approval queue, branch claude/step2-draft-queue-*). To avoid merge
conflicts, follow these boundaries strictly:
- Work on branch claude/step3-publish.
- Do NOT create or edit db.py, config.py, config.yaml, ingest/, filter/,
  score/, run_ingest.py, run_score.py, or digest.py. Those belong to step 1.
- Do NOT create or edit draft/, queue/, run_draft.py, or run_queue.py. Those
  belong to step 2.
- You may add ONE dependency to pyproject.toml (tweepy) and one line to
  [tool.setuptools] py-modules for your CLI. Do not otherwise modify it. Ask
  before adding anything else.
- Put your own storage in publish/store.py, creating your own tables with
  CREATE TABLE IF NOT EXISTS in the same SQLite file (path from env var
  DB_PATH, default ./pipeline.db). Never modify tables you did not create.
- Read approved drafts through ONE adapter function,
  publish/store.py:fetch_approved(limit) -> list[Approved], that queries the
  step 2 drafts table. Assume this schema and note it in a comment so it is
  easy to reconcile with step 2 when the branches merge:
    drafts(id INTEGER PK, cluster_id, source, url, single_post TEXT,
           thread_json TEXT, status TEXT,   -- 'pending'|'approved'|'rejected'|'snoozed'
           created_at, updated_at)
  If a decisions row exists with edited_text for the draft, prefer
  edited_text over single_post. Assume:
    decisions(id INTEGER PK, draft_id FK, action, original_text, edited_text,
              decided_at)
  Also assume items/clusters/scores from step 1 as they exist on main
  (scores keys on cluster_id, not item_id); read from them only to fill
  breaking-news detection (source starts with 'fda' or 'company_').

Build this structure:

  publish/
    __init__.py
    client.py         # ONE function post_tweet(text, in_reply_to=None) ->
                      #   tweet_id and ONE function verify_credentials();
                      #   these are the only places tweepy is imported.
                      #   Keys from .env: X_API_KEY, X_API_SECRET,
                      #   X_ACCESS_TOKEN, X_ACCESS_SECRET. Retry/backoff on
                      #   429 and 5xx; never retry on 403 (duplicate) or
                      #   401.
    scheduler.py      # pure functions: next_slot(now, slots, tz) ->
                      #   datetime; pick_for_slot(approved, slot, policy)
                      #   -> Approved | None; is_breaking(approved) -> bool.
                      #   Slots, timezone, max posts/day, min gap between
                      #   posts, and breaking-news sources live in
                      #   publish/config.yaml (your own file, NOT the root
                      #   config.yaml). Load it with pyyaml.
    store.py          # posts table (draft_id, tweet_id, text, kind
                      #   'single'|'thread', position, posted_at, slot,
                      #   error) + schedule table (draft_id, scheduled_for,
                      #   claimed_at) + fetch_approved adapter
    thread.py         # split thread_json into ordered posts; enforce
                      #   <= 280 chars with URLs counted as 23; append
                      #   "(n/N)" only if it still fits; last post must
                      #   contain the source URL (never strip it)
  run_publish.py      # CLI. Default mode is --dry-run: print what WOULD be
                      #   posted and when, post nothing. --live posts only
                      #   if env PUBLISH_ENABLED=1 AND --live is passed.
                      #   Flags: --now (ignore slots, post the top
                      #   candidate), --breaking (post only breaking items),
                      #   --limit N. Designed to be run from cron every
                      #   15 min; it must be idempotent: a draft is posted at
                      #   most once even if cron fires twice (claim the
                      #   schedule row in a transaction before posting).
  tests/test_publish_*.py

Requirements:
- Safety first. Nothing posts unless PUBLISH_ENABLED=1 AND --live. Tests
  must assert that dry-run never calls post_tweet, and that a second run
  after a successful post does not post again.
- Threads post in order with in_reply_to chained to the previous tweet_id.
  If post k of a thread fails, record posts 1..k-1 with their tweet_ids,
  store the error on post k, mark the draft status in YOUR posts table as
  'partial', and stop. Do not delete already-posted tweets. Do not retry a
  partial thread automatically; log it loudly for the human.
- Every posted text is re-checked in code before posting: <= 280 chars
  (URL = 23), contains the source URL where required, no text edits beyond
  what thread.py does. Refuse and log if a check fails; never "fix" content
  here, that is the approval queue's job.
- Breaking items (FDA approvals, company PRs with 'approv' in the title)
  may post outside slots but still respect the min-gap and daily cap.
- Bio disclosure of AI-assisted drafting is a manual step; add a README
  section and a startup warning in run_publish.py if a
  BIO_DISCLOSURE_CONFIRMED=1 env var is not set. Do not automate bio edits.
- All X API calls go through publish/client.py so tests can monkeypatch
  post_tweet. Tests never hit the network and never import tweepy at
  module import time (import it inside the functions).
- Tests: scheduler slot math across a DST boundary, thread splitting and
  the 280 rule, the URL-in-last-post rule, idempotent claim under a
  simulated double run, partial-thread failure handling, breaking-news
  detection, and the PUBLISH_ENABLED gate; all against a temp SQLite file
  seeded with fake drafts/decisions rows.
- Read secrets from .env via python-dotenv. Logging via stdlib: INFO for
  each decision (skipped/scheduled/posted), WARNING for refusals, ERROR
  for API failures. Never log full API keys.
- ruff check, ruff format --check, and pytest must pass before every commit.

Start by proposing the posts/schedule table schema and publish/config.yaml,
then implement scheduler.py and thread.py with tests (they are pure and need
no DB), then store.py, then client.py, then run_publish.py. Commit as you
go and push to your branch.

Do not build the feedback loop (impressions/engagement pull); that is
step 4. Do not touch the approval queue UI.
