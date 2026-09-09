You are building step 7 of the pipeline described in PLAN.md: the voice
learning loop. PLAN.md's approval-queue stage says "Edit history is saved as
voice-guide training data", and step 2 does save it (every edit and rejection
in the approval UI writes a `decisions` row with original_text, edited_text
and a note), but nothing reads it back: the drafter sends the same static
voice.md every run, the human's ~10 minutes of daily edits are write-only,
and there is no way to see what the model keeps getting wrong. That is this
step: recent human edits and rejection reasons become few-shot examples in
the drafting prompt, the queue records WHY a draft was edited or rejected,
and a voice report tells the human which voice.md changes the edits are
asking for. Read PLAN.md and CLAUDE.md first.

Other sessions are working IN PARALLEL on other branches: step 4 (feedback
loop, package feedback/, CLI run_feedback.py, branch claude/step4-feedback or
claude/parallel-prompt-4-*), step 5 (operations layer, package ops/, CLI
run_ops.py, deploy/, branch claude/step5-ops or claude/parallel-prompt-5-*)
and step 6 (new ingest sources and HTTP retry, ingest/, config.py,
config.yaml, run_ingest.py, branch claude/step6-sources or
claude/parallel-prompt-6-*). Steps 1, 2 and 3 are merged on main. All three
parallel prompts forbid their sessions from touching step 2's files, so
step 2's draft and approval-queue layer is yours to extend. To avoid merge
conflicts, follow these boundaries strictly:
- Work on branch claude/step7-voice.
- You MAY create or edit: draft/ (new modules, additive changes to
  draft/prompt.py and draft/drafter.py that keep every existing signature
  and default behaviour so existing tests pass unchanged, and a new
  draft/config.yaml), approval_queue/ (store.py, app.py, templates/),
  run_draft.py, run_queue.py, tests/test_draft_*.py, tests/test_queue_*.py,
  tests/test_run_draft.py, and new tests/test_voice_*.py files.
- Do NOT create or edit db.py, config.py, config.yaml, ingest/, filter/,
  score/, run_ingest.py, run_score.py, digest.py (step 1, and step 6 is
  editing config.py / config.yaml / ingest/ right now); publish/,
  run_publish.py (step 3); feedback/, run_feedback.py (step 4, in
  progress); ops/, run_ops.py, deploy/ (step 5, in progress);
  .github/workflows/ci.yml; or any existing tests/test_*.py file outside
  the list above.
- Do NOT touch pyproject.toml at all (steps 4 and 5 are both editing the
  same two lines of it). No new dependency: everything you need is in the
  stdlib (difflib, json, sqlite3, collections) plus pyyaml, fastapi, jinja2
  and python-dotenv, which are already there. This step adds no run_*.py
  module: the report CLI is `python -m draft.voice_report`, and draft* is
  already in packages.find. Ask before adding anything.
- Do NOT edit draft/voice.md. The whole point of this step is that the
  human changes voice.md by hand after reading the report; the code only
  proposes. Do not edit score/rubric.py or filter/prefilter.py either
  (rubric tuning is step 4's report).
- README.md and .env.example: APPEND one clearly labelled section/block at
  the end of each ("## Voice learning (step 7)" / "# Step 7: voice"); do
  not otherwise edit them, so they merge next to steps 4, 5 and 6's
  additions. DRAFT_MODEL already exists in .env.example; do not add it
  again. If merging main produces a conflict there, keep both sides.
- CLAUDE.md: additive edits only (the draft/ and approval_queue/ lines in
  Layout, one rule bullet about examples never overriding hard rules).
  Keep both sides on conflict.
- Tables: you own `drafts` and `decisions` (step 2's) and any table you
  create. Adding a nullable column to `decisions` is allowed ONLY through a
  guarded migration in approval_queue/store.py:connect (PRAGMA table_info,
  then ALTER TABLE ... ADD COLUMN; never a destructive rebuild), because
  steps 4 and 5 read `decisions` through adapters that select named
  columns and must keep working on both old and new databases. Never
  modify tables you do not own.

Nothing in this step posts anything. It calls the Anthropic API only where
step 2 already does (draft/drafter.py:call_anthropic), with a longer prompt.

Build this structure:

  draft/
    config.yaml       # your own file, NOT the root config.yaml (step 6 is
                      #   editing that). Contents:
                      #   examples:
                      #     enabled: true
                      #     lookback_days: 60
                      #     max_examples: 6        # BEFORE/AFTER pairs
                      #     max_rejections: 4      # rejected post + reason
                      #     max_chars_per_post: 600
                      #     min_change_ratio: 0.08 # 1 - difflib ratio;
                      #                            # typo-level edits teach
                      #                            # nothing and are skipped
                      #     skip_categories: [factual, hard_rule]
                      #   report:
                      #     weeks: 4
                      #     top_pairs: 10
                      #     propose_banned_after: 3   # a phrase the human
                      #                               # deleted this many
                      #                               # times -> proposed
                      #                               # banned phrase
                      #     stopwords: [the, a, an, and, or, of, to, in, ...]
                      #   model: claude-sonnet-5   # fallback only, see below
                      #   Load with pyyaml in one function load_draft_config().
    examples.py       # pure, no DB, no network:
                      #   @dataclass EditExample(decision_id, draft_id,
                      #     source, category, note, created_at,
                      #     original_single, original_thread: list[str],
                      #     edited_single, edited_thread: list[str])
                      #   @dataclass RejectionExample(decision_id, draft_id,
                      #     source, category, note, created_at, single_post)
                      #   parse_decision_text(text) -> (single, thread):
                      #     inverse of approval_queue.store._serialise_text
                      #     (JSON {"single_post", "thread"}); a bare string
                      #     (approve/reject/snooze rows store single_post
                      #     only) parses as (text, []).
                      #   select_edit_examples(rows, cfg, *, now) ->
                      #     list[EditExample]: only action 'edit' rows whose
                      #     edited_text differs from original_text; within
                      #     lookback_days; category not in skip_categories;
                      #     change ratio (1 - difflib.SequenceMatcher ratio
                      #     over single_post + thread) >= min_change_ratio;
                      #     the EDITED text must pass
                      #     draft.drafter.check_hard_rules for its own url
                      #     and source (a human edit that slipped in advice
                      #     or dropped the URL is never taught); newest
                      #     first; at most max_examples; each post truncated
                      #     to max_chars_per_post with a visible "[...]".
                      #   select_rejections(rows, cfg, *, now) ->
                      #     list[RejectionExample]: action 'reject' rows that
                      #     have a category or a non-empty note, newest
                      #     first, at most max_rejections. Rejections with
                      #     category 'hard_rule' or 'factual' are kept here
                      #     (they are useful as "avoid" signals) but the
                      #     note is what the model sees, never a diagnosis.
                      #   format_examples_block(edits, rejections) -> str |
                      #     None: None when both lists are empty. Otherwise
                      #     a block headed "=== RECENT HUMAN EDITS ===" with
                      #     numbered "BEFORE (model):" / "AFTER (human):" /
                      #     "WHY: <note or category>" entries (single post,
                      #     then the thread only if it changed), then
                      #     "=== RECENTLY REJECTED ===" entries with the post
                      #     and the reason, then one closing line: these
                      #     show the reviewer's taste; the HARD RULES below
                      #     always win over any example. Deterministic
                      #     output for the same input (tests compare
                      #     strings).
    prompt.py         # Additive. build_system_prompt(examples_block: str |
                      #   None = None) and build_prompt(..., examples_block=
                      #   None): when None the output is byte-identical to
                      #   today's; when given, the block goes AFTER the
                      #   voice guide and BEFORE HARD_RULES and the schema,
                      #   so the hard rules are the last thing the model
                      #   reads. The block lives in the system prompt, not
                      #   the user prompt, and run_draft builds it ONCE per
                      #   run so it is identical across the items of a run
                      #   (this keeps the system prompt stable; no prompt
                      #   caching work is required in this step).
    drafter.py        # Additive. draft_item(..., examples_block: str | None
                      #   = None) passes it through to build_prompt. Model
                      #   resolution replaces the hardcoded DEFAULT_MODEL
                      #   literal (CLAUDE.md forbids claude-* IDs in code):
                      #   DRAFT_MODEL env, else root config.yaml
                      #   models.drafter if the human has added that key
                      #   (read via config.load_config(); do NOT add the key
                      #   to config.yaml yourself, step 6 owns it; say in
                      #   README that the human may add it), else
                      #   draft/config.yaml model. check_hard_rules and
                      #   flag_unverified_numbers are unchanged and still
                      #   run on every output: examples can never relax
                      #   them.
    voice_report.py   # pure functions + a small CLI:
                      #   @dataclass VoiceReport(window_start, window_end,
                      #     drafts_total, approved_unedited, edited,
                      #     rejected, snoozed, failed, edit_rate,
                      #     by_source: list[(source, drafts, edited,
                      #     rejected)], by_category: list[(category, n)],
                      #     length_delta_median (chars, single post),
                      #     thread_dropped_rate (edits where the human
                      #     shortened the thread), banned_phrase_hits:
                      #     list[(phrase, n)] found in ORIGINAL texts using
                      #     the "## Banned phrases" list parsed from
                      #     voice.md (parse_banned_phrases(voice_md); strip
                      #     quotes and the parenthetical "unless ..." tails),
                      #     deleted_words / added_words: top 15 lowercased
                      #     words by net count across all edits, stopwords
                      #     removed, numbers and URLs removed,
                      #     top_pairs: list[EditExample], rejection_notes:
                      #     list[(reason, n)], proposals: list[Proposal(kind,
                      #     text, evidence)] where kind is 'banned_phrase'
                      #     (a non-stopword phrase of 1-3 words deleted >=
                      #     propose_banned_after times), 'length' (median
                      #     delta < -40 chars -> "posts are running long"),
                      #     'thread' (thread_dropped_rate > 0.5 -> "threads
                      #     are being cut; lead with the single post"),
                      #     'tone' (a note word such as hype/jargon/vague
                      #     recurring >= 3 times). Every proposal says what
                      #     to paste where (draft/voice.md, which section)
                      #     and changes nothing.
                      #   build_report(drafts_rows, decisions_rows, voice_md,
                      #     cfg, *, now, weeks) -> VoiceReport
                      #   render_markdown(report) -> str with a fixed
                      #     caveats block (small-n; edits reflect one
                      #     reviewer's taste; hard rules are enforced in
                      #     code, not learned).
                      #   CLI (`python -m draft.voice_report`):
                      #     --weeks N (default from config) --out FILE
                      #     --examples (print the exact block the next
                      #     run_draft would send, then exit) --json.
                      #     Reads the DB only through approval_queue.store
                      #     adapters; no network.
  approval_queue/
    store.py          # Additive:
                      #   - guarded migration adding decisions.category TEXT
                      #     (nullable); DECISION_CATEGORIES = ("voice",
                      #     "factual", "not_newsworthy", "hard_rule",
                      #     "other"); edit(...) and reject(...) gain
                      #     category: str | None = None and validate it.
                      #   - new table draft_examples(id PK, draft_id
                      #     REFERENCES drafts, decision_id REFERENCES
                      #     decisions, kind 'edit'|'rejection', created_at)
                      #     so every draft records which examples it was
                      #     shown (the audit trail for "did the examples
                      #     help", which step 4's report can join later).
                      #     record_examples(conn, draft_id, edit_ids,
                      #     rejection_ids).
                      #   - adapters: fetch_decisions_for_voice(conn, since)
                      #     -> decisions joined with drafts (status,
                      #     created_at) and items (source, url) via
                      #     drafts.item_id, oldest first, tolerant of a DB
                      #     without step 1 tables (source/url empty);
                      #     fetch_draft_stats(conn, since) -> drafts rows for
                      #     the report (id, item_id, source, status,
                      #     created_at). These are the ONLY places step 7
                      #     reads step 1's items table, and only for source
                      #     and url.
    app.py            # Additive: the reject form and the edit form get a
                      #   <select name="category"> over DECISION_CATEGORIES
                      #   (blank allowed); GET /voice renders the voice
                      #   report from live data (weeks from ?weeks=, default
                      #   from config) as HTML tables via a template, no
                      #   markdown library; the detail page's decision
                      #   history shows category and, for edits, a
                      #   line-level before/after diff (difflib.ndiff in a
                      #   <pre>, added/removed lines classed for colour);
                      #   the index gets a "voice report" link. No JS
                      #   framework, server-rendered as before.
    templates/voice.html, additive edits to detail.html, index.html, base.html
  run_draft.py        # Additive: loads draft/config.yaml; unless
                      #   --no-examples or examples.enabled is false, fetches
                      #   decisions via fetch_decisions_for_voice, builds the
                      #   block ONCE with select_edit_examples /
                      #   select_rejections / format_examples_block, logs
                      #   "voice examples: N edits, M rejections (last D
                      #   days)" at INFO, passes examples_block to every
                      #   draft_item call, and after each successful
                      #   insert_draft calls record_examples. --dry-run
                      #   prints the block length and example decision ids.
                      #   Existing flags and behaviour unchanged.
  tests/test_draft_examples.py, tests/test_voice_report.py,
  tests/test_queue_voice.py, additive tests in tests/test_draft_prompt.py,
  tests/test_draft_drafter.py, tests/test_queue_store.py,
  tests/test_queue_app.py, tests/test_run_draft.py

Requirements:
- Examples never weaken the hard rules. Three layers, each tested: an
  edited text that fails check_hard_rules is not selected as an example;
  HARD_RULES appear after the examples block in the system prompt; and
  draft_item still runs check_hard_rules on every output with examples
  present (a fake call that returns advice text with examples in the
  prompt is still rejected).
- Backward compatibility, tested: build_prompt without examples_block
  returns exactly the same (system, user) strings as before this step
  (assert equality against a call with examples_block=None and against a
  saved copy of the pre-step system prompt minus the schema dump if you
  prefer); draft_item's existing call signature works; connect() on a
  database created with the OLD decisions schema (create it by hand in the
  test without the category column) migrates it, and connect() on a new
  database is idempotent across two calls; approve/edit/reject/snooze
  without a category still work.
- Example selection tests with hand-built rows: an edit identical to the
  original is skipped; a one-character edit is skipped by min_change_ratio;
  a 'factual' edit is skipped; an edit older than lookback_days is skipped;
  newest first and capped at max_examples; long posts are truncated with
  "[...]"; a thread that did not change is omitted from the entry; the
  formatted block is deterministic and None when nothing qualifies;
  parse_decision_text round-trips _serialise_text and accepts a bare
  string.
- Voice report tests with hand-built rows: edit rate with zero drafts (no
  ZeroDivisionError, rate None); banned phrase hits found in an original
  containing "game-changer" using a voice.md snippet; deleted/added word
  lists exclude stopwords, numbers and URLs; a phrase deleted three times
  yields a 'banned_phrase' proposal and twice does not; median length delta
  of -60 yields a 'length' proposal; the "no edits yet" state renders
  cleanly with the caveats block; the markdown contains every section
  heading; --json output is valid JSON.
- Queue tests against a temp SQLite file: reject with category 'voice'
  stores it and an invalid category raises ValueError; edit with category;
  record_examples writes rows and fetch_decisions_for_voice returns source
  and url from items when step 1 tables exist and empty strings when they
  do not; GET /voice returns 200 with and without data; the detail page of
  an edited draft shows the diff with the removed line marked; posting the
  reject form with a category persists it.
- End-to-end test in tests/test_run_draft.py: seed step 1 tables (db.py
  init) and step 2 tables, an old draft with an 'edit' decision, and a new
  scored candidate; run run_draft.main with a monkeypatched
  draft.drafter.call_anthropic (or draft_item) that captures the system
  prompt; assert the prompt contains "RECENT HUMAN EDITS", the BEFORE and
  AFTER texts, and HARD_RULES after them; assert draft_examples has a row
  linking the new draft to that decision; rerun with --no-examples and
  assert the prompt has no examples block and no draft_examples row is
  added.
- Model resolution test: with DRAFT_MODEL unset and a monkeypatched
  config.load_config returning {"models": {"drafter": "x"}}, model_name()
  is "x"; with neither, it is draft/config.yaml's model; DRAFT_MODEL wins
  over both. No claude-* literal remains in draft/drafter.py (grep in a
  test).
- Read secrets from .env via python-dotenv. Logging via stdlib: INFO for
  example counts per run and report headline counts, DEBUG for which
  decision ids were selected, WARNING when an edit was skipped because
  its edited text fails a hard rule (this means the human published-ready
  text breaks a rule; say which). Never log API keys.
- Generated content must never contain medical advice; the report and the
  UI summarise edits and counts and quote the human's own text only.
  Preprint labelling is unchanged.
- ruff check, ruff format --check, and pytest must pass before every commit.

Start by proposing draft/config.yaml, the EditExample / RejectionExample /
VoiceReport / Proposal dataclasses, the decisions.category migration and
the draft_examples table, then implement examples.py with tests (pure, no
DB), then the prompt.py / drafter.py additions with the backward-
compatibility tests, then store.py (migration, adapters, record_examples),
then voice_report.py, then the queue UI and run_draft.py wiring with the
end-to-end test. Commit as you go and push to your branch.

Do not build a new run_*.py CLI, do not edit voice.md, the scoring rubric or
the prefilter, do not touch the ingest layer, the publish layer, the
feedback loop or the ops layer, and do not post anything.
