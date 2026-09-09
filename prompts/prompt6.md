You are building step 6 of the pipeline described in PLAN.md: the two
ingest sources PLAN.md lists that nobody has built yet, plus the hardening
the ingest layer needs to carry them. Step 1 ingests PubMed, bioRxiv/medRxiv,
ClinicalTrials.gov, FDA and ~35 company feeds; it does NOT ingest the
ASCO / AACR / ESMO / ASH abstract releases or the curated X list of oncology
KOLs, and its HTTP layer has no retry, so one transient 503 costs a whole
cadence. That is this step: a Crossref-backed conference-abstract source with
meeting-window cadence, an X-list source for KOL posts, and retry/backoff in
`ingest/http.py`. Read PLAN.md and CLAUDE.md first.

Other sessions are working IN PARALLEL on other branches: step 4 (feedback
loop, package feedback/, CLI run_feedback.py, branch claude/step4-feedback or
claude/parallel-prompt-4-*) and step 5 (operations layer, package ops/, CLI
run_ops.py, deploy/, branch claude/step5-ops or claude/parallel-prompt-5-*).
Steps 1, 2 and 3 are merged on main. Both parallel prompts forbid their
sessions from touching step 1's files, so step 1's ingest layer is yours to
extend. To avoid merge conflicts, follow these boundaries strictly:
- Work on branch claude/step6-sources.
- You MAY create or edit: ingest/ (new modules, and additive changes to
  ingest/http.py and ingest/base.py that keep every existing signature and
  behaviour so existing tests pass unchanged), config.py, config.yaml,
  run_ingest.py (register the new source types only), new
  tests/test_ingest_*.py files and new tests/fixtures/ files.
- Do NOT create or edit db.py, filter/, score/, run_score.py, or digest.py.
  The conference flood is handled inside the source (keyword gate, per-run
  cap, priority ordering), not by changing prefilter or scoring policy. If
  you believe a prefilter change is needed, describe it in README instead.
- Do NOT create or edit draft/, approval_queue/, run_draft.py, run_queue.py
  (step 2); publish/, run_publish.py (step 3); feedback/, run_feedback.py
  (step 4, in progress); ops/, run_ops.py, deploy/ (step 5, in progress);
  .github/workflows/ci.yml; or any existing tests/test_*.py file unless a
  change you made to shared code genuinely requires it (it should not; keep
  signatures backward compatible instead).
- Do NOT touch pyproject.toml at all. No new dependency: httpx, feedparser,
  pyyaml, python-dotenv and pydantic are already there, ingest* is already
  in packages.find, and this step adds no CLI. Ask before adding anything.
- README.md and .env.example: APPEND one clearly labelled section/block at
  the end of each ("## Conference abstracts and KOL list (step 6)" /
  "# Step 6: sources"); do not otherwise edit them, so they merge next to
  steps 4 and 5's additions. Step 4 is adding X_BEARER_TOKEN to
  .env.example; your block adds X_KOL_LIST_ID and CROSSREF_MAILTO and a
  comment that X_BEARER_TOKEN is shared with the feedback loop. If merging
  main produces a conflict there, keep both sides and drop any duplicated
  X_BEARER_TOKEN line.
- CLAUDE.md: additive edits only (the ingest line in Layout, the
  network-confinement rule, the config-expansion rule). Keep both sides on
  conflict.

Nothing in this step calls the Anthropic API or posts anything. The X list
source is READ-ONLY against the X API and disabled by default.

Build this structure:

  ingest/
    http.py           # Extend, do not rewrite. get_text/get_json gain
                      #   optional keyword args headers: dict | None and
                      #   max_attempts: int (default from a module constant,
                      #   3). Bounded retry with exponential backoff on
                      #   429 and 5xx and on httpx transport errors
                      #   (ConnectError, ReadTimeout); honour a numeric
                      #   Retry-After header, capped by a max wait
                      #   constant; never retry other 4xx. The retry loop
                      #   lives in one private function _request(...) that
                      #   calls httpx.get, so tests monkeypatch httpx.get
                      #   and time.sleep. The DEBUG log line must print the
                      #   URL and params only, never headers (a bearer
                      #   token will pass through here).
    base.py           # Additive: Source.is_due gains meeting-window
                      #   awareness. A source config may carry
                      #   windows: [{start: YYYY-MM-DD, end: YYYY-MM-DD,
                      #   cadence_minutes: N}]; when `now` (UTC date) falls
                      #   inside a window, inclusive, that window's cadence
                      #   replaces cadence_minutes. No windows -> exactly
                      #   the current behaviour. Also add an `enabled`
                      #   pass-through note: config.py must copy
                      #   enabled: false into every expanded source so
                      #   step 5's health check does not report a
                      #   deliberately disabled source as "never ran".
    crossref.py       # CrossrefSource, type "crossref". Fetches works from
                      #   the Crossref REST API (api_url from config,
                      #   default https://api.crossref.org/works) with
                      #   filter=issn:<issn>,from-created-date:<lookback>,
                      #   rows=<rows>, cursor=* deep paging (follow
                      #   message.next-cursor for up to max_pages), a
                      #   select= list of the fields you use, and
                      #   mailto=<crossref.mailto> for the polite pool
                      #   (CROSSREF_MAILTO env overrides). Network only via
                      #   http.get_json in ONE method fetch_page(params) so
                      #   tests monkeypatch it. Pure function
                      #   parse_works(items, source_name, cfg) -> list[Item]:
                      #   url = https://doi.org/<DOI>, doi = DOI, title =
                      #   first title, abstract = JATS abstract with
                      #   <jats:...> tags stripped via ingest.rss.strip_html,
                      #   published_at = created date-time (fall back to
                      #   issued date-parts), raw_json = {container-title,
                      #   volume, issue, type, meeting, session_hint}.
                      #   Keep only works whose `issue` (or DOI) matches the
                      #   source's issue_pattern regex (this is what
                      #   separates a supplement of meeting abstracts from
                      #   the journal's regular issues). Then a keyword
                      #   gate on title+abstract (source `keywords`,
                      #   defaulting to prefilter.allow_keywords), then
                      #   priority ordering: works whose DOI or title
                      #   matches any boost_patterns (e.g. LBA, plenary,
                      #   late-breaking) come first, then newest created,
                      #   then truncate to max_items_per_run. Prepend one
                      #   line to the abstract, e.g. "ASCO 2027 Annual
                      #   Meeting abstract (Journal of Clinical Oncology
                      #   45, 16_suppl)." so the scorer and drafter see the
                      #   meeting context; leave the title untouched so the
                      #   later full publication can still join the same
                      #   cluster by DOI or near-duplicate title.
    x_list.py         # XListSource, type "x_list". Reads recent posts from
                      #   ONE X list (the curated KOL list, created by hand
                      #   on X) via GET <api_url>/lists/<list_id>/tweets
                      #   with tweet.fields=created_at,public_metrics,
                      #   entities,referenced_tweets,author_id,
                      #   expansions=author_id, user.fields=username,name,
                      #   max_results (<= 100), start_time = now minus
                      #   lookback_hours (the source has no DB access;
                      #   lookback_hours must exceed cadence_minutes so
                      #   runs overlap, and dedup_hash makes the overlap
                      #   harmless), and pagination_token for up to
                      #   max_pages. App-only
                      #   auth: Authorization: Bearer <X_BEARER_TOKEN> from
                      #   .env, passed as headers= to http.get_json in ONE
                      #   method fetch_page(params, token). list_id from
                      #   config kol.list_id, X_KOL_LIST_ID env overrides.
                      #   If the source is enabled and the token or list_id
                      #   is missing, raise RuntimeError with a clear
                      #   message BEFORE any network call so run_ingest
                      #   records it in source_runs.error. Never log the
                      #   token. Pure function parse_tweets(payload,
                      #   source_name, cfg) -> list[Item]: skip retweets and
                      #   replies (referenced_tweets types, each behind a
                      #   config flag), skip posts with no external link
                      #   when require_link is true (a link to x.com or
                      #   t.co does not count), skip authors not in
                      #   kol.handles when enforce_handles is true; url =
                      #   https://x.com/<username>/status/<id>, title =
                      #   "@<username>: " + first 120 chars of text,
                      #   abstract = full text with t.co links replaced by
                      #   entities.urls[].expanded_url, plus a "Links:"
                      #   line; doi = first DOI found in the expanded URLs
                      #   (ingest.base.extract_doi) so a KOL post about a
                      #   paper joins that paper's cluster; published_at =
                      #   created_at; raw_json = author, metrics, links,
                      #   referenced_tweets. Provide a pure
                      #   estimated_requests_per_month(cfg) -> int =
                      #   runs/month x max_pages, used by a test and logged
                      #   at INFO on each fetch.
  config.py           # Extend load_config's expansion exactly like
                      #   _company_sources: `conferences.meetings` expands
                      #   into sources conf_<key>_abstracts (type crossref,
                      #   with issn, issue_pattern, boost_patterns,
                      #   keywords, lookback_days, rows, max_pages,
                      #   max_items_per_run, cadence_minutes =
                      #   conferences.default_cadence_minutes, windows from
                      #   the meeting with conferences.window_cadence_minutes,
                      #   enabled) and, when news_rss is set,
                      #   conf_<key>_news (type rss, same windows);
                      #   `kol` expands into ONE source kol_x_list (type
                      #   x_list, enabled: false unless kol.enabled). The
                      #   duplicate-name check must cover the new sources.
  config.yaml         # New top-level sections (keep every existing key):
                      #   crossref: {api_url, mailto, rows, max_pages}
                      #   conferences:
                      #     default_cadence_minutes: 1440   # outside windows
                      #     window_cadence_minutes: 60      # inside a window
                      #     max_items_per_run: 40
                      #     boost_patterns: [LBA, late-breaking, plenary]
                      #     meetings:            # one per society, e.g.
                      #       - key: asco  name: "ASCO Annual Meeting"
                      #         journal: "Journal of Clinical Oncology"
                      #         issn: 0732-183X  issue_pattern: "suppl"
                      #         windows: [{start, end}]   # abstract-release
                      #                   # day through meeting end + 3 days
                      #         news_rss: <society newsroom feed, if one
                      #                    verified live>
                      #       - aacr (Cancer Research supplement),
                      #         esmo (Annals of Oncology supplement),
                      #         ash  (Blood supplement)
                      #   kol:
                      #     enabled: false      # needs X API read access
                      #     list_id: ""         # X_KOL_LIST_ID overrides
                      #     api_url: https://api.x.com/2
                      #     cadence_minutes: 120  max_results: 100
                      #     max_pages: 1  lookback_hours: 6
                      #     require_link: true  skip_retweets: true
                      #     skip_replies: true  enforce_handles: false
                      #     monthly_request_cap: 500
                      #     handles: ~50 entries of {handle, name, focus}
                      #   Every URL, ISSN, pattern and date lives here, not
                      #   in code. Windows carry a comment with the society
                      #   page you took the dates from and "update yearly".
  run_ingest.py       # Add CrossrefSource and XListSource to SOURCE_TYPES.
                      #   Nothing else changes.
  tests/test_ingest_http.py, tests/test_ingest_windows.py,
  tests/test_ingest_crossref.py, tests/test_ingest_x_list.py,
  tests/test_ingest_config6.py
  tests/fixtures/crossref_*.json (real), tests/fixtures/x_list_tweets.json
  (synthetic)

Requirements:
- Verify live before committing config. Crossref is bot-friendly: for each
  society, query the supplement ISSN for the most recent meeting and
  confirm (a) the supplement works are identifiable by issue_pattern and
  (b) they carry an `abstract`. Save one real page as the crossref fixture
  (the JCO ASCO supplement is the likely candidate). A society whose
  supplement records have no abstracts in Crossref gets enabled: false and
  a comment saying so (the prefilter drops abstract-less items anyway);
  its newsroom RSS, if one is verified live, stays on. Meeting windows come
  from the society sites; put the URL in a comment. ESMO 2026 and ASH 2026
  are still ahead as of this writing (September 2026): make sure their
  windows are in.
- The X list endpoint needs a bearer token, so its fixture is synthetic but
  shaped like a real /2/lists/:id/tweets response: data[] with entities.urls
  (url, expanded_url), referenced_tweets, public_metrics, author_id;
  includes.users[]; meta.next_token; include one retweet, one reply, one
  post with only an x.com link, one with a doi.org link, one from an
  author not in the handles list.
- http retry tests monkeypatch httpx.get with a fake that returns a scripted
  sequence of responses and monkeypatch time.sleep to record waits: 503 then
  200 succeeds in two calls; 429 with Retry-After: 7 waits 7 s; a 404 is
  raised on the first attempt with no retry; max_attempts failures raise
  the last error; headers reach httpx.get; caplog at DEBUG never contains
  the header value.
- Window tests use explicit `now` datetimes: no windows -> unchanged
  behaviour; inside a window the window cadence applies; the start and end
  days are inclusive; the day after the end falls back to the default
  cadence; a disabled source is never due.
- Crossref tests: parse the real fixture (DOI, https://doi.org URL, stripped
  JATS abstract, created date, the meeting line prepended, title untouched);
  a work whose issue does not match issue_pattern is dropped; the keyword
  gate drops a non-oncology title; boost ordering puts an LBA first;
  max_items_per_run truncates; fetch paginates by next-cursor and stops at
  max_pages, and every call carries mailto and the ISSN filter.
- X list tests: parse skips the retweet, the reply, the x.com-only post and
  (with enforce_handles) the unknown author; the doi.org post gets doi set;
  the abstract contains the expanded URL, not the t.co one; fetch sends the
  Authorization header and start_time and stops at max_pages; a missing
  token raises RuntimeError before fetch_page is called (assert with a
  fetch_page that fails the test if invoked); estimated_requests_per_month
  for the shipped config.yaml is <= kol.monthly_request_cap.
- Config tests: load_config on the shipped config.yaml yields
  conf_<key>_abstracts sources for every meeting with the expected windows,
  kol_x_list with enabled False, no duplicate names, and no source of type
  rss whose URL is empty; load_config on a temp yaml with an enabled kol
  block yields enabled True.
- End-to-end test through run_ingest.ingest with monkeypatched http: a
  crossref source inserts abstract items and clusters; a later rss item
  with the same DOI joins the abstract's cluster (one cluster, two
  sources); a KOL post whose expanded link is doi.org/<that DOI> joins it
  too, and filter.prefilter.cluster_text still returns the paper's
  abstract, not the tweet text (longest wins). A second run inside the
  cadence fetches nothing; a run with `now` inside a meeting window fetches
  again after window_cadence_minutes.
- Fail soft per source, as step 1 does: any exception in fetch is recorded
  in source_runs.error and the run continues; parse functions skip
  malformed records with a DEBUG line rather than raising.
- Read secrets from .env via python-dotenv. Logging via stdlib: INFO for
  per-source counts (works fetched / kept after issue filter / kept after
  keywords / capped; tweets fetched / skipped by reason / kept; estimated
  monthly requests), DEBUG for items. Never log the bearer token or any
  Authorization header.
- The KOL handles list is documentation and an optional filter: ~50 public,
  active oncology accounts (trialists, cell and gene therapy scientists,
  FDA/OCE and journal-editor accounts, biotech analysts), each with a
  one-line focus. Public professional accounts only; no patients, no
  private individuals. The human curates the actual X list by hand; note
  in README that the list must be created on X and its id put in
  X_KOL_LIST_ID, that reading it requires X API read access (a paid tier;
  the budget is shared with step 4's feedback snapshots, which is why
  kol.monthly_request_cap exists), and that kol.enabled stays false until
  then.
- Generated content is unchanged by this step: items feed the existing
  prefilter, scorer and drafter. The meeting line prepended to abstracts
  is factual metadata only. Preprint labelling is unaffected (conference
  abstracts are not preprints; the scorer's evidence_level handles them).
- ruff check, ruff format --check, and pytest must pass before every commit.

Start by proposing the config.yaml additions (crossref, conferences, kol)
and the expansion rules in config.py, then implement the http.py retry and
the base.py windows with tests (no fixtures needed), then crossref.py with
the live-captured fixture, then x_list.py with the synthetic fixture, then
the config expansion, run_ingest registration and the end-to-end test.
Commit as you go and push to your branch.

Do not build a CLI, do not change prefilter or scoring policy, do not touch
the approval queue UI, the publish layer, the feedback loop or the ops
layer, and do not post, like, follow or write anything on X.
