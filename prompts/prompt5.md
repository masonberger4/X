You are building step 5 of the pipeline described in PLAN.md: the operations
layer that lets the whole thing run unattended on a small VPS under cron.
PLAN.md's six pipeline stages are all built or claimed (steps 1-4 below), but
nothing yet runs them in order, detects when a source or stage has silently
stopped, backs up the database, or tells the human when something needs
attention. That is this step: an orchestrator, health checks, alerts,
backups, and deploy files. Read PLAN.md and CLAUDE.md first.

Other sessions are working IN PARALLEL on other branches: step 3 (publish
layer, package publish/, CLI run_publish.py, branch claude/step3-publish or
claude/prompt3-review-*) and step 4 (feedback loop, package feedback/, CLI
run_feedback.py, branch claude/step4-feedback or claude/parallel-prompt-4-*).
Steps 1 and 2 are merged on main. To avoid merge conflicts, follow these
boundaries strictly:
- Work on branch claude/step5-ops.
- Do NOT create or edit db.py, config.py, config.yaml, ingest/, filter/,
  score/, run_ingest.py, run_score.py, or digest.py (step 1); draft/,
  approval_queue/, run_draft.py, or run_queue.py (step 2); publish/ or
  run_publish.py (step 3, in progress); feedback/ or run_feedback.py (step 4,
  in progress). Do not edit .github/workflows/ci.yml.
- Do NOT add any dependency. Everything you need is in the stdlib plus
  pyyaml, python-dotenv and httpx, which are already in pyproject.toml. The
  only pyproject.toml change allowed is adding "run_ops" to
  [tool.setuptools] py-modules and "ops*" to packages.find include. Ask
  before adding anything else.
- Put your own storage in ops/store.py, creating your own tables with
  CREATE TABLE IF NOT EXISTS in the same SQLite file. Resolve the path the
  same way approval_queue/store.py:db_path does: DB_PATH env var, else
  config.yaml db_path, else ./pipeline.db. Never modify, prune or vacuum
  tables you did not create; every other step's data is read-only to you.
  (Retention for items/scores/drafts is a policy the owning step decides;
  you may only REPORT table sizes.)
- Do not import ingest/, score/, draft/, publish/ or feedback/ modules. The
  orchestrator runs the other steps as subprocesses by their CLI names, so
  a step whose module is not yet merged (publish, feedback) is simply
  skipped with a WARNING on this checkout and starts working when it lands.
- README.md and .env.example: APPEND one clearly labelled section/block at
  the end of each ("## Operations (step 5)" / "# Step 5: ops"); do not
  otherwise edit them, so they merge next to steps 3 and 4's additions.
  If merging main produces a conflict there, keep both sides.

Read other steps' tables only through adapter functions in ops/store.py,
each read-only, each with the assumed schema in a comment so it is easy to
reconcile when branches merge, and each returning an empty result (never
raising) when the table does not exist yet:

  fetch_source_runs() -> list[SourceRun]
    Step 1, as merged on main:
      source_runs(source TEXT PK, last_run_at TEXT, fetched INTEGER,
                  inserted INTEGER, error TEXT)   -- one row per source,
                                                  -- overwritten each run
    Also read the configured source list (names + cadence_minutes +
    enabled) via config.load_config() so a source that is enabled but has
    NO source_runs row at all is reported as "never ran".

  fetch_stage_activity() -> StageActivity
    Step 1 + step 2, as merged on main:
      items(id, source, url, doi, title, abstract, published_at, fetched_at,
            dedup_hash, cluster_id, raw_json)
      clusters(id, ..., created_at, prefilter_status 'pass'|'drop'|NULL,
               prefilter_reason)
      scores(id, cluster_id, model, prompt_version, ..., total, scored_at)
      drafts(id, item_id, cluster_id, model, single_post, thread_json, ...,
             status 'pending'|'approved'|'rejected'|'snoozed', ...,
             snoozed_until, created_at, updated_at)
      decisions(id, draft_id, action, original_text, edited_text, note,
                created_at)
    Returns: latest fetched_at, latest scored_at, latest drafts.created_at,
    latest decisions.created_at, count of prefilter-passed clusters with no
    score row, count of pending drafts and the age of the oldest, count of
    approved drafts, scores rows in the last 24h, drafts rows in the last
    24h.

  fetch_publish_state() -> PublishState
    Step 3 has NOT merged yet; assume the schema on its branch:
      schedule(id, draft_id UNIQUE, scheduled_for, claimed_at, finished_at,
               status 'pending'|'claimed'|'posted'|'partial'|'refused'|
               'failed', error)
      posts(id, draft_id, tweet_id, text, kind 'single'|'thread', position,
            posted_at, slot, status 'posted'|'failed', error)
    Returns: posts in the last 24h / 7d, schedule rows with status
    'partial' or 'failed' in the last 7d (these need a human), rows stuck in
    'claimed' for more than an hour (a crashed run), latest posted_at.

  fetch_feedback_state() -> FeedbackState
    Step 4 has NOT merged yet; assume the schema its kickoff prompt
    (prompts/prompt4.md) specified:
      tweet_metrics(id, tweet_id, draft_id, captured_on, captured_at, ...)
      follower_snapshots(id, captured_on UNIQUE, captured_at, followers, ...)
      feedback_reports(id, window_start, window_end, generated_at, ...)
    Returns: latest captured_on, latest follower snapshot date, latest
    report generated_at.

Build this structure:

  ops/
    __init__.py
    config.yaml       # your own file, NOT the root config.yaml. Contents:
                      #   steps: ordered list of {name, argv (list),
                      #     enabled, required, timeout_seconds}. Default
                      #     order: ingest, score, draft, publish, feedback.
                      #     ingest/score/draft are enabled+required.
                      #     publish is enabled: false with argv
                      #     ["python", "run_publish.py"] (dry-run, the
                      #     CLI's default) and a comment that the human
                      #     enables it and adds --live only after reading
                      #     README's publishing section; feedback snapshot
                      #     is enabled: false, not required.
                      #   lock_path, run_log_tail_chars (how much stdout/
                      #     stderr to keep per run), health thresholds
                      #     (max hours since ingest / score / draft, max
                      #     source-failure fraction, pending-draft max age
                      #     hours, unscored backlog max, scores-per-day and
                      #     drafts-per-day budget caps derived from PLAN.md's
                      #     $30-60/mo, db max MB, disk min free MB, backup
                      #     max age hours), backups (dir, keep), alerts
                      #     (cooldown_hours, channels enabled flags,
                      #     webhook_timeout_seconds). Load with pyyaml.
    lock.py           # acquire(path) -> Lock | None using fcntl.flock on a
                      #   lock file that stores the holder's pid and
                      #   started_at. A second acquire returns None (do not
                      #   block). A lock whose pid is no longer alive is
                      #   reclaimed with a WARNING. Context-manager API.
    runner.py         # run_steps(steps, *, only=None, dry_run=False,
                      #   env=None, python=sys.executable) ->
                      #   list[StepResult(name, argv, started_at, finished_at,
                      #   exit_code, timed_out, skipped_reason, stdout_tail,
                      #   stderr_tail)]. Runs each enabled step as a
                      #   subprocess with the step's timeout; argv[0] ==
                      #   "python" is replaced by the running interpreter.
                      #   If the step's CLI file (argv[1]) does not exist
                      #   on this checkout, skip with skipped_reason=
                      #   "not merged" and a WARNING. A failing required
                      #   step stops the run (later steps get
                      #   skipped_reason="upstream failed"); a failing
                      #   optional step is logged and the run continues.
                      #   Pure w.r.t. the DB: the caller records results.
    health.py         # pure functions over the dataclasses above, no DB,
                      #   no network, no clock reads (now is a parameter):
                      #   check_sources, check_staleness, check_backlog,
                      #   check_budget, check_publish, check_feedback,
                      #   check_backups, check_storage (db size, disk free,
                      #   both passed in), check_env (names of required
                      #   env vars that are missing, given a
                      #   {name: bool} map; never values). Each returns
                      #   Check(name, status 'ok'|'warn'|'fail'|'skip',
                      #   summary, details: dict). run_all(...) -> Report
                      #   (checked_at, overall status = worst check,
                      #   checks). 'skip' is for a step whose tables do not
                      #   exist yet and never counts against overall.
    alert.py          # notify(report, prior, cfg) decides what to send and
                      #   sends it. Channels: log (always; WARNING for warn,
                      #   ERROR for fail), webhook (generic JSON POST of
                      #   {"text": ...} to env ALERT_WEBHOOK_URL, which
                      #   works for Slack/Discord/Mattermost incoming
                      #   webhooks; the ONLY outbound network call in ops/,
                      #   in one function post_webhook(url, payload) so
                      #   tests monkeypatch it), email (stdlib smtplib +
                      #   email.message; SMTP_HOST/PORT/USER/PASSWORD,
                      #   ALERT_EMAIL_FROM/TO from .env; in one function
                      #   send_email(...) so tests monkeypatch it).
                      #   Cooldown: a check that is still failing with the
                      #   same status as the last stored alert is not
                      #   re-sent until cooldown_hours pass; a check that
                      #   recovers to ok sends one "recovered" message.
                      #   Never include secrets, post text, or abstracts
                      #   in an alert; check names, summaries and counts
                      #   only.
    backup.py         # backup(db_path, dest_dir, keep) -> Path using the
                      #   sqlite3 online backup API (Connection.backup),
                      #   named pipeline-YYYYmmddTHHMMSSZ.sqlite, then
                      #   PRAGMA integrity_check on the copy (delete it and
                      #   raise if not "ok"), then rotate so only `keep`
                      #   newest remain. latest_backup(dest_dir) ->
                      #   (Path, datetime) | None. restore is a documented
                      #   manual step (copy the file back), not code.
    store.py          # tables you own:
                      #   pipeline_runs(id PK, run_id TEXT, step TEXT,
                      #     argv TEXT, started_at, finished_at, exit_code,
                      #     timed_out INTEGER, skipped_reason TEXT,
                      #     stdout_tail TEXT, stderr_tail TEXT)
                      #   health_checks(id PK, checked_at, overall TEXT,
                      #     report_json TEXT)
                      #   alerts_sent(id PK, check_name, status, sent_at,
                      #     channel)
                      #   + the four read-only adapters above
                      #   + last_run_per_step(), last_health(),
                      #     last_alert_per_check(), prune_own(days)
                      #     (deletes only from these three tables).
  run_ops.py          # CLI with subcommands, designed for cron:
                      #   run       take the lock, run the configured steps
                      #             in order, record every StepResult, then
                      #             run health + alerts. --only a,b runs a
                      #             subset; --dry-run prints the argv it
                      #             would run and records nothing; exit
                      #             code 1 if a required step failed, 2 if
                      #             the lock was held, 0 otherwise.
                      #   health    run checks, print a plain-text report,
                      #             --json for machine output, --alert to
                      #             also send notifications, exit code 1 if
                      #             overall is 'fail'.
                      #   backup    run backup.backup; --keep N override.
                      #   status    one screen: last run per step with exit
                      #             code and age, last health overall,
                      #             table row counts (read-only), latest
                      #             backup age, disk free.
                      #   prune     --days N delete old rows from ops-owned
                      #             tables only.
  deploy/
    crontab.example   # run every 30 min, backup daily, health --alert
                      #   hourly, prune weekly; all with `cd` to the repo
                      #   and the venv python; comments explaining that
                      #   publish stays dry-run until enabled in
                      #   ops/config.yaml and PUBLISH_ENABLED=1 is set.
    pipeline.service, pipeline.timer   # systemd equivalents of the cron
                      #   `run` entry (oneshot service + 30-min timer),
                      #   with EnvironmentFile pointing at .env.
    README.md         # VPS setup in ~15 lines: venv, .env, crontab -e or
                      #   systemctl enable --now, where the lock file,
                      #   backups and logs live, how to restore a backup.
  tests/test_ops_*.py

Requirements:
- Safety first. Nothing in ops/ ever posts to X, calls the Anthropic API,
  or edits content. The default ops/config.yaml must not contain "--live"
  anywhere, and a test asserts it. `run --dry-run` executes no subprocess;
  a test asserts that with a monkeypatched subprocess.run.
- The orchestrator must be safe under overlapping cron fires: a test runs
  acquire twice and the second returns None; a lock file with a dead pid is
  reclaimed; a lock file with the live test pid is not.
- Runner tests use tiny real subprocesses (`python -c ...` via
  sys.executable) for: success, non-zero exit, timeout (a step that sleeps
  longer than its timeout is killed and timed_out=True), a required
  failure stopping later steps, an optional failure not stopping them, and
  a missing CLI file being skipped with "not merged". stdout/stderr tails
  are truncated to run_log_tail_chars.
- Health checks are pure and unit-tested with hand-built dataclasses:
  a source failing vs never-ran vs ok; staleness just inside and just
  outside each threshold; backlog and budget caps; publish 'partial' rows
  producing 'fail'; a step with no tables producing 'skip' that does not
  affect overall; env check listing missing names and never echoing a
  value; storage thresholds.
- Alerts are tested with monkeypatched post_webhook/send_email: a fail
  sends once, the same fail inside the cooldown does not send again, after
  the cooldown it sends again, recovery sends one "recovered" message, and
  the payload never contains any value from os.environ (assert against a
  sentinel env var set in the test). Missing ALERT_WEBHOOK_URL / SMTP_*
  means that channel is skipped with an INFO line, not an error.
- Backup tests run against a temp SQLite file: the copy opens and passes
  integrity_check, rotation keeps exactly `keep` files, a corrupt copy is
  deleted and raises, and latest_backup returns None on an empty dir.
- End-to-end test against a temp SQLite file: create step 1 tables with
  db.py's Database init and step 2 tables with approval_queue.store's
  connect; create the assumed step 3 schedule/posts tables by hand in the
  fixture; seed a few source_runs (one with an error), items/clusters/
  scores/drafts rows and one 'partial' schedule row; run `run_ops.py
  health --json` via main(argv) and assert the JSON reports the failing
  source, the partial thread as 'fail', and feedback as 'skip'; run `run
  --only ingest --dry-run` and assert pipeline_runs stays empty; run
  `status` and assert it prints without error.
- Read secrets from .env via python-dotenv. Logging via stdlib: INFO for
  each step start/finish with duration and exit code, WARNING for skipped
  steps, lock reclaim and 'warn' checks, ERROR for failed steps and 'fail'
  checks. Never log secrets.
- Generated text (alerts, reports, status) must never contain medical
  content; it summarises pipeline state, not science.
- ruff check, ruff format --check, and pytest must pass before every commit.

Start by proposing the three tables, ops/config.yaml, and the
SourceRun/StageActivity/PublishState/FeedbackState/Check/Report
dataclasses, then implement lock.py and runner.py with tests (they need no
DB), then health.py, then store.py with the adapters, then backup.py,
alert.py, run_ops.py and deploy/. Commit as you go and push to your branch.

Do not build or change any pipeline stage, do not add ingest sources (the
ASCO/AACR/ESMO/ASH abstract feeds and the KOL X list from PLAN.md are a
later prompt), do not touch the approval queue UI, and do not automate
anything that publishes.
