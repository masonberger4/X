You are building step 2 of the pipeline described in PLAN.md: the drafting
layer and the human approval queue. Read PLAN.md and CLAUDE.md first.

Another session is building step 1 (ingest + score) IN PARALLEL on a
different branch. To avoid merge conflicts, follow these boundaries strictly:
- Work on branch claude/step2-draft-queue.
- Do NOT create or edit db.py, config.yaml, ingest/, score/, run_ingest.py,
  run_score.py, or digest.py. Those belong to step 1.
- You may add dependencies to pyproject.toml (fastapi, uvicorn, jinja2) but
  do not otherwise modify it. Ask before adding anything else.
- Put your own storage in queue/store.py, creating your own tables with
  CREATE TABLE IF NOT EXISTS in the same SQLite file (path from an env var
  DB_PATH, default ./pipeline.db). Never modify the items or scores tables.
- Read scored items through ONE adapter function,
  queue/store.py:fetch_candidates(min_score, since_hours) -> list[Candidate],
  that queries the items and scores tables. Assume this schema and note it
  in a comment so it is easy to reconcile with step 1 when the branches merge:
    items(id TEXT PK, source, url, title, abstract, published_at, fetched_at,
          dedup_hash UNIQUE, raw_json)
    scores(item_id FK, novelty, clinical_significance, audience_interest,
           expertise_fit, timeliness, total, rationale, suggested_angle,
           scored_at)

Build this structure:

  draft/
    __init__.py
    voice.md          # voice guide: tone, 5-8 sample posts, banned phrases,
                      #   the originality rule (interpretation required)
    prompt.py         # builds the drafting prompt from voice.md + item +
                      #   suggested_angle; embeds the hard rules
    schema.py         # pydantic-free: dataclass + JSON schema for output:
                      #   single_post, thread (3-6 posts), suggested_visual,
                      #   why_it_matters, claims_to_verify (list of
                      #   {claim, confidence: high|medium|low})
    drafter.py        # calls Anthropic API (claude-sonnet-5 by default,
                      #   model from env DRAFT_MODEL); one item per call;
                      #   retry/backoff; validates output against schema
  queue/
    __init__.py
    store.py          # drafts + decisions tables; fetch_candidates adapter
    app.py            # FastAPI + Jinja2, server-rendered, no JS framework:
                      #   list pending drafts with source, score, rationale;
                      #   detail page showing single_post, thread,
                      #   claims_to_verify; actions approve / edit / reject /
                      #   snooze (snooze = hide for 24h)
    templates/
  run_draft.py        # CLI: fetch candidates above threshold, draft any
                      #   without an existing draft, store them as pending
  run_queue.py        # CLI: uvicorn launcher for queue/app.py on localhost
  tests/test_draft_*.py, tests/test_queue_*.py

Requirements:
- Hard rules baked into the drafting prompt AND checked in code after
  generation: no medical advice or treatment recommendations; primary
  source URL must appear in single_post and in the last thread post;
  preprints (source is biorxiv/medrxiv) are labelled "preprint"; every
  number in the draft must appear verbatim in the source abstract, otherwise
  flag it in claims_to_verify with confidence low. Reject drafts that fail
  and log why.
- Every single_post and every thread post must be <= 280 characters. Count
  URLs as 23 characters (t.co). Test this.
- Edits made in the approval UI are saved as a decisions row with
  original_text, edited_text, and action, so they can later train the voice
  guide. Approved drafts are stored with status=approved and are NOT
  published; publishing is step 3.
- All Anthropic calls go through one function so tests can mock them.
  Tests never hit the network. Test: schema validation, the 280 rule, the
  preprint label rule, the number-verification rule, and each queue action
  against a temp SQLite file seeded with fake items/scores rows.
- Read ANTHROPIC_API_KEY from .env via python-dotenv. Logging via stdlib.
- ruff check, ruff format --check, and pytest must pass before every commit.

Start by proposing the drafts/decisions table schema and the output JSON
schema, then implement draft/schema.py and draft/prompt.py with tests, then
drafter.py, then the queue. Commit as you go and push to your branch.

Do not build posting, the X API integration, or the feedback loop.
