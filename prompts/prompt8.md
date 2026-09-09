PROJECT BUILD COMPLETE

There is no prompt 8. Every stage in PLAN.md's pipeline, and the stack it
asks for, is either merged on main or claimed by a session that is building
it right now under strict file-ownership boundaries. Writing another kickoff
prompt would mean inventing scope that PLAN.md does not ask for.

Audit of PLAN.md against the repo (main at the time of writing; steps 5, 6
and 7 on their own branches, see prompts/prompt5.md .. prompt7.md):

  PLAN.md section                  Status
  -------------------------------  ------------------------------------------
  1. Ingest: PubMed E-utilities    step 1, merged (ingest/pubmed.py)
     bioRxiv / medRxiv RSS         step 1, merged (ingest/biorxiv.py)
     ClinicalTrials.gov            step 1, merged (ingest/clinicaltrials.py)
     FDA press releases + OCE      step 1, merged (ingest/rss.py, fda_oce.py)
     ~40 company PR/IR feeds       step 1, merged (config.yaml companies)
     ASCO/AACR/ESMO/ASH abstracts  step 6, in progress (ingest/crossref.py)
     Curated X list of ~50 KOLs    step 6, in progress (ingest/x_list.py)
     SQLite store, dedup_hash,     step 1, merged (db.py, filter/dedup.py)
     skip seen items
  2. Score: 0-10 rubric, bonus,    step 1, merged (score/); every score row
     threshold, log every score    logged with model + prompt_version
  3. Draft: voice guide, JSON      step 2, merged (draft/); hard rules also
     output, hard rules            enforced in code (check_hard_rules)
  4. Approval queue: web UI,       step 2, merged (approval_queue/); edits
     approve/edit/reject/snooze,   saved as decisions rows; step 7 (in
     edit history as voice data    progress) reads them back into the prompt
                                   and produces the voice report
  5. Publish: slots, breaking,     step 3, merged (publish/); bio disclosure
     bio discloses AI              is a documented manual step in README
  6. Feedback loop: impressions,   step 4, merged (feedback/); reports
     engagement, follower delta,   PROPOSE rubric/prefilter/slot changes
     feed back into the rubric
  Stack: cron or a small VPS       step 5, in progress (ops/, deploy/)
  Stack: budget $30-60/mo          step 5 health check caps scores/drafts
                                   per day (a count proxy, deliberately)
  Build order 1: "read the digest  digest.py --rate, merged; the reading is
  for a week before posting"       the human's job, not code

Things that were considered and rejected as prompt 8, with the reason, so
a later reader does not reopen them by accident:

- A Telegram/Slack approval bot. PLAN.md says "web UI OR Telegram/Slack
  bot"; the web UI exists and step 5's webhook alerts already push
  "needs attention" messages to Slack/Discord. A second approval surface
  would duplicate approval_queue/ and the same DB tables step 7 is editing.
- Per-call API cost accounting (token usage rows). Not a PLAN.md stage.
  It would have to edit score/scorer.py and draft/drafter.py; step 7 owns
  drafter.py right now, so it cannot run in parallel without conflicts,
  and step 5's count-based caps already cover the stated budget.
- Image generation for suggested_visual. PLAN.md asks for a suggested
  visual as text, which the drafter produces and the queue shows.
- Progress tracking against the Original Content Rewards thresholds. The
  API does not expose Premium-user impressions (feedback/report.py says
  so); follower delta is already reported. Nothing further can be measured.
- Prompt caching, retention/pruning of step 1-3 tables, or a settings UI.
  Optimisations, not pipeline stages; each would touch files another
  in-flight step owns.

What remains is operational, not code: merge steps 5, 6 and 7 once their
CI is green (they were written to merge without conflicts: disjoint
packages, append-only README/.env.example blocks, additive CLAUDE.md
edits, pyproject.toml touched only by step 5); then the human runs the
digest for a week, curates the KOL list on X, fills in .env, edits the
account bio, and flips PUBLISH_ENABLED=1 with --live when ready. If a
genuinely new requirement appears later (a new source, a new channel),
write it as its own prompt against the then-current main rather than as a
continuation of this series.
