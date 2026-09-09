# How to use this project, step by step

Windows commands, run from `C:\Users\you\X` in a Command Prompt. On Mac or
Linux the only differences are `python3` for `python`, `cp` for `copy`, and
`cat` for `type`. Every command that talks to Claude uses your Claude Code
login (`LLM_BACKEND=claude_code` in `.env`); nothing posts to X until part 5.

---

## Part 1. One-time setup (done once per computer)

1. Clone the code and install it.
   ```
   git clone https://github.com/masonberger4/X.git
   cd X
   pip install -e ".[dev]"
   ```
2. Create your private settings file and open it.
   ```
   copy .env.example .env
   notepad .env
   ```
   Set these lines and save:
   ```
   LLM_BACKEND=claude_code
   DRAFT_MODEL=
   NCBI_EMAIL=you@example.com
   ```
   Optional, free: an NCBI API key on `NCBI_API_KEY=` and your email on
   `CROSSREF_MAILTO=`. Leave the X lines blank until part 5.
3. Install Claude Code and log in (needs Node.js from nodejs.org first).
   ```
   npm install -g @anthropic-ai/claude-code
   claude login
   echo say ok | claude -p --output-format json --model claude-opus-5
   ```
   The last line must print `"result":"ok"` inside the output.
4. Confirm the install.
   ```
   ruff check .
   pytest
   ```
   Expect "All checks passed" and several hundred tests passing.

---

## Part 2. The daily loop (about 15 minutes a day)

Run these in order, once a day. Extra runs are harmless: ingest refetches a
source only when it is due, and score only scores what is new.

1. Get the latest code (only needed when told there is an update).
   ```
   git pull
   ```
2. Fetch news from every source.
   ```
   python run_ingest.py
   ```
   Errors from `clinicaltrials_oncology` and the FDA approvals page are
   expected from a home connection.
3. Filter and score what came in.
   ```
   python run_score.py
   ```
   Prints a start and a finish line per batch of ten. Up to 150 stories a day
   are scored; the rest wait for tomorrow. Do not click inside the window
   while it runs (see "Fixing things").
4. Read the top stories with the model's rating beside each, and add yours.
   ```
   python digest.py --auto-rate --rate
   ```
   The model rates first (a number and one-line reason per entry), then you
   are asked for a rating 1 to 5 per entry:
   - `5` would post today, `4` worth a post, `3` interesting but no,
     `2` marginal, `1` noise
   - `s` skips one, `q` quits, Enter after the note when it asks for one
   Rate on whether YOU would post it. Your notes are read by me when tuning.
5. Optional views of the same list.
   ```
   python digest.py                     # print only, no rating
   python digest.py --all --hours 72    # ignore the score threshold, 3 days
   python digest.py --out digest.md     # save to a file
   ```

---

## Part 3. Drafting and approving posts (start after a few days of part 2)

1. Draft posts for the top stories.
   ```
   python run_draft.py
   ```
   Writes one draft (a single post plus a 3 to 6 post thread) for up to 10
   stories from the last 48 hours scoring at or above the digest threshold
   (30 of 50, from `config.yaml`). Drafts that break a hard rule (advice,
   made-up numbers, missing source link, too long) are stored as failed, not
   shown.
   ```
   python run_draft.py --dry-run                 # show what would be drafted
   python run_draft.py --min-score 38 --limit 5  # only the strongest few
   python run_draft.py --retry-failed            # try again on stories whose draft failed
   ```
2. Check the claims. Each draft lists the facts the model added from its own
   knowledge (competitor pipelines, deal terms, cost claims). This step sends
   each one to Claude with web search on, which finds a primary source and
   quotes the sentence that supports or contradicts it.
   ```
   python run_verify.py
   ```
   ```
   python run_verify.py --dry-run     # list the claims, no calls
   python run_verify.py --redo        # check again, replacing old verdicts
   python run_verify.py --draft 12    # one draft
   ```
   About one to two minutes per claim. Verdicts are only "verified" when
   the source is on a trusted site (`verify\config.yaml`, plus every company
   site in `config.yaml`); anything else is shown as a lead. Once the
   scheduler in part 6 is running, this happens automatically after every
   drafting run, so by the time you open the queue the evidence is already
   attached; running it by hand is only for drafts you made by hand.
3. Open the approval page.
   ```
   python run_app.py
   ```
   Then open http://localhost:8000/queue in a browser (the front page is the
   dashboard; part 8 explains it). `python run_queue.py` still opens the
   approval page on its own if that is all you want. For each draft: approve,
   edit, reject or snooze. When you edit or reject, pick a reason (voice,
   factual, not newsworthy, hard rule, other). Press Ctrl+C in the window to
   stop the server when done.
   Under "Claims to verify" each claim shows its verdict, the source link and
   the quoted sentence. Open the link and read the sentence before approving;
   the verdict is a lead, the link is the proof. A contradicted claim blocks
   Approve until you edit the draft or tick "approve anyway".
4. After a couple of weeks, see what your edits are asking for and paste the
   suggestions you agree with into `draft\voice.md`.
   ```
   python -m draft.voice_report
   python -m draft.voice_report --weeks 8 --out voice.md
   ```
   The approval page also has this at http://localhost:8000/voice

---

## Part 4. Set up the X account (do once, in parallel with part 3)

1. Create or choose the X account. Write the bio by hand; it must say posts
   are AI-assisted and that nothing is investment advice. Then in `.env`:
   ```
   BIO_DISCLOSURE_CONFIRMED=1
   ```
2. Go to https://developer.x.com, sign in with that account, create a
   project and an app, and generate the four posting keys. Paste them into
   `.env`:
   ```
   X_API_KEY=
   X_API_SECRET=
   X_ACCESS_TOKEN=
   X_ACCESS_TOKEN_SECRET=
   ```
   The free tier is enough to post. Leave `X_BEARER_TOKEN=` blank; it needs
   a paid tier and is only for reading metrics (part 7).
3. Optional: X Premium on the account, required for creator payouts.

---

## Part 5. Publishing

1. Rehearse. With no flags nothing is sent; it prints what would post and
   when, based on approved drafts and the slots in `publish\config.yaml`.
   ```
   python run_publish.py
   ```
   Run this a few times over a couple of days until the plan looks right.
2. Go live. Two things are required, so nothing posts by accident: in `.env`
   ```
   PUBLISH_ENABLED=1
   ```
   and the `--live` flag:
   ```
   python run_publish.py --live
   ```
   Start with a handful of approved drafts, not a backlog.
3. Useful variants.
   ```
   python run_publish.py --live --now        # ignore slots, post the top candidate once
   python run_publish.py --live --breaking   # only FDA / company-approval items
   python run_publish.py --live --limit 1    # at most one post this run
   ```
   A draft is posted at most once even if the command runs twice. A thread
   that fails part-way is marked partial and left for you; it is never
   retried automatically.

---

## Part 6. Running it on a schedule (Windows Task Scheduler)

1. Try the orchestrator by hand first. It runs ingest, score, draft and
   verify in order under a lock, and records each step.
   The `verify` step runs after `draft` and is optional: if it fails, the
   claims show as "not checked yet" and the run carries on.
   ```
   mkdir logs
   python run_ops.py run --dry-run
   python run_ops.py run
   python run_ops.py status
   python run_ops.py health
   python run_ops.py backup
   ```
2. Open a Command Prompt as Administrator and create the tasks:
   ```
   schtasks /Create /TN "pipeline-run" /SC MINUTE /MO 30 /TR "cmd /c cd /d C:\Users\you\X && python run_ops.py run >> logs\ops.log 2>&1"
   schtasks /Create /TN "pipeline-health" /SC HOURLY /TR "cmd /c cd /d C:\Users\you\X && python run_ops.py health --alert >> logs\ops.log 2>&1"
   schtasks /Create /TN "pipeline-backup" /SC DAILY /ST 03:00 /TR "cmd /c cd /d C:\Users\you\X && python run_ops.py backup >> logs\ops.log 2>&1"
   ```
   In the Task Scheduler app, open each task and tick "Run whether user is
   logged on or not" and "Wake the computer to run this task". The PC must be
   on for them to fire.
3. Publishing is off in the scheduler until you enable it. When ready, edit
   `ops\config.yaml` and change the `publish` step from
   ```
     argv: ["python", "run_publish.py"]
     enabled: false
   ```
   to
   ```
     argv: ["python", "run_publish.py", "--live"]
     enabled: true
   ```
   with `PUBLISH_ENABLED=1` already in `.env`. Until then, publishing stays a
   command you run by hand.
4. Alerts (optional): put a Slack or Discord incoming-webhook URL on
   `ALERT_WEBHOOK_URL=` in `.env`, or fill the `SMTP_*` and `ALERT_EMAIL_*`
   lines, and the hourly health task will message you when a source or step
   stops working.
5. Check on it.
   ```
   python run_ops.py status
   type logs\ops.log
   schtasks /Query /TN pipeline-run
   ```

---

## Part 7. Weekly feedback (needs the paid X API read tier)

1. Put the bearer token on `X_BEARER_TOKEN=` in `.env`.
2. Daily and weekly:
   ```
   python run_feedback.py snapshot          # metrics for recent posts, once a day
   python run_feedback.py report            # last week, printed
   python run_feedback.py report --weeks 4 --out report.md
   python run_feedback.py followers         # follower time series
   ```
   The report proposes changes to the scoring rubric, prefilter keywords,
   posting slots and voice guide. It applies none of them; tell me which you
   want and I will commit them.

---

## Part 8. The control panel (one window for everything)

```
python run_app.py
```
Open http://localhost:8000. Leave it running in its own window; press Ctrl+C
to stop it. Four pages:

- **Dashboard** (`/`) — the same health checks `python run_ops.py health`
  prints, worst first, plus the last outcome of every scheduled step, how
  many rows are in each table, the database size, free disk and the age of
  the latest backup. This is the page to open when something looks wrong.
- **Sources** (`/sources`) — every source from `config.yaml`: when it last
  ran, how many items it fetched, how many were new, and the last error if
  it failed. A source in red has been failing; one in amber has not run for
  three times its cadence.
- **Feed** (`/feed`) — the same list `python digest.py` prints: the top scored
  stories of the last 24 hours, each with its score breakdown, the reason it
  scored that way and a link to the source. Rate the ones you have an opinion
  about 1 to 5 and add a note. This is the same rating `digest.py --rate`
  asks for at the terminal, and it is what the scoring gets tuned against
  later. It does not change what gets drafted today. Use the window links to
  look back 72 hours or a week, or to ignore the score threshold.
- **Publishing** (`/publishing`) — how many drafts are approved and waiting,
  what has gone out, and anything that needs a human (a thread that stopped
  halfway is never retried for you). Read-only: there is no post button here.
- **Feedback** (`/feedback`) — followers over time, your posts ranked by
  impressions, and the suggestions from the latest weekly report. The
  suggestions are proposals only; applying one means editing a settings file.
- **Runs** (`/runs`) — tick the steps you want and press "Run selected
  steps". The log appears on the page as it finishes. This runs exactly what
  the scheduler in part 6 runs; if the scheduler happens to be running at
  that moment the page says so and does nothing, rather than running twice.
- **Pending / Approved / Snoozed / Rejected / Failed / Voice report** — the
  approval pages from part 3, unchanged.

The one thing it writes outside its own pages is a rating on the feed page.
Everything else is a view.

Two things it deliberately will not do. It never posts to X: publishing is
off in `ops\config.yaml` and stays off, and there is no publish button. And
it never edits settings: change `config.yaml` or `draft\voice.md` on disk
(ask me to commit it), not in the browser.

Anyone who can reach the page can run the pipeline, so keep it on
`localhost`. `--host` and `--port` move it and `--reload` is for development;
only use `--host 0.0.0.0` on a network you trust.

---

## Fixing things

| Symptom | What to do |
|---|---|
| `Not logged in` or `OAuth session expired` in a score, draft or rate run | `claude login`, then rerun the command |
| A run prints nothing for many minutes, then everything at once | the console was paused by a click (press Enter or Esc; untick "QuickEdit Mode" in the window's Properties) or the PC slept (`powercfg /change standby-timeout-ac 0`) |
| `git pull` refuses because you edited a file | `git checkout <file>` to discard, or ask me to commit the change |
| Scoring says `scoring 0 clusters` right after a keyword change | `python run_score.py --refilter` |
| Scoring says `pass: 0, deferred: N` | today's cap of 150 is spent; the N wait for tomorrow, or raise `daily_cap` in `config.yaml` |
| A source keeps erroring | it is logged and skipped; the others still run. Paste the line to me |
| Want a completely fresh start | delete `pipeline.db`, then `python run_ingest.py --force` |
| The dashboard says `missing env: ANTHROPIC_API_KEY` but you use the Claude Code backend | it should not since the check follows `LLM_BACKEND`; make sure `.env` is in the folder you start `run_app.py` from |
| The control panel says a run is already in progress | the scheduler (part 6) is mid-run; wait for it and press the button again |
| The control panel will not start: `Address already in use` | another `run_app.py` or `run_queue.py` window is open; close it or use `--port 8001` |

## Changing settings

Everything lives in `config.yaml` (sources, keywords, models, caps),
`draft\config.yaml` (how human edits are reused), `verify\config.yaml`
(the fact-checking model and the trusted source sites), `publish\config.yaml`
(posting slots, daily post cap, breaking-news rules), `feedback\config.yaml`
and `ops\config.yaml` (which steps the scheduler runs). Ask me to commit a change rather than editing by
hand, so your copy and GitHub stay in step.
