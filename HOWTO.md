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
   pip install -e ".[dev,images]"
   ```
   `images` is the chart drawing library (matplotlib) for the picture that goes
   with a post (part 3, step 1). Without it everything still works, posts
   just go out text-only.
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
4. Read the top stories with the model's decision beside each, and add yours.
   ```
   python digest.py --auto-rate --rate
   ```
   The model decides first (yes or no and a one-line reason per entry), then
   you are asked the same question per entry, as the editor:
   - `y` you would post about it, `n` you would not
   - `s` skips one, `q` quits
   - after `y` or `n` a box lists the reason categories (beat, company,
     catalyst, evidence, thesis, business, coverage, hype) and asks why. The
     explanation is required: start it with the deciding category, for
     example `catalyst: PDUFA in Q4, public sponsor` or `beat: solid tumour
     ADC, not our modality`.
   Decide on whether YOU would post it. Your explanations are what the
   scoring gets tuned against, so the deciding reason matters more than the
   yes or no. Decisions are stored in `ratings` as 5 (yes) or 1 (no), so
   ratings made on the old 1 to 5 scale still count (4 and 5 read as yes).
5. Optional views of the same list.
   ```
   python digest.py                     # print only, no decisions
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
   A draft may come with a picture. Nothing draws it freehand: when the source
   has two or more comparable numbers (arms, endpoints, cohorts) the drafter
   lists them as a small bar-chart spec, every number in it is checked
   against the source the same way the post text is (one miss and the chart
   is dropped, with a note under "Claims to verify"), and the code draws the
   chart to `images\draft_<id>.png` next to `pipeline.db`. "Suggested visual"
   on the draft page is still just the model's one-line idea for you; the
   chart is what actually gets attached. `images: enabled: false` in
   `draft\config.yaml` turns the drawing off.
   The other kind of picture is a comparison table (a competitor landscape,
   a catalyst list, deal terms side by side): 2-8 rows, 2-5 columns, the
   first column naming the company, asset or trial. Unlike a chart its cells
   may come from the model's own knowledge, so it is NOT drawn here: step 2
   fact-checks every cell on the web first (see below). Until then the draft
   page shows the cells and says the picture is pending.
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
   The same run then handles comparison tables: every cell that is not
   verbatim in the source article is one more web-search call (the row label
   and column header are sent with it, so "Agenus, Stage: Phase 2" stands on
   its own). Once every cell has a verdict the table is drawn to
   `images\draft_<id>.png` with a blank where a cell could not be tied to a
   trusted primary source (the footer says so), or dropped, on the draft's
   record, when a cell was contradicted, fewer than `min_supported_ratio` of
   the fact cells passed, fewer than two rows kept a verified label, or the
   table had more than `max_cells_per_draft` cells. `tables:` in
   `verify\config.yaml` holds those knobs and `enabled: false` skips tables
   altogether (they then stay unrendered and are dropped at approval).
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
   revise, reject or snooze. Press Ctrl+C in the window to stop the server
   when done.
   To change a post, do not retype it: write what should change in the
   "what should change?" box (on the list next to each draft, or under
   "Revise" on the draft's page) and press Revise. The drafter rewrites the
   post from your note, keeps the rest as it was, the hard rules are checked
   again in code, and the draft comes back to you still pending with a
   before/after diff in its history. It takes 10-30 seconds: a yellow status
   bar at the top of the page shows while the drafter works, the form is
   locked so it cannot be sent twice, and the page reloads by itself. If the model
   cannot produce a valid rewrite the draft is left untouched and the page
   says why. Pick a reason (voice, factual, not newsworthy, hard rule, other)
   when you revise or reject; your notes feed the voice report. "Edit by hand"
   is still there, folded away, for a one-word fix. A save that breaks a rule (a post
   over 280 characters, URLs counting as 23, or an empty single post) is refused on the
   same page: the reason sits at the top, your text stays in the boxes, and nothing is
   saved or approved. The same goes for Approve while a claim is contradicted.
   If the draft has a chart it is shown under "Image" with the exact text a
   screen reader will get (the alt text), and it is attached to the first
   post when published. Check every bar against the source like any other
   number. "Drop image" posts the text alone; a Revise redraws the chart from
   the new draft (or removes it if the new draft has none).
   A table shows under "Table cells" with each cell's verdict and source link
   (green: kept; amber: blanked in the picture; red: contradicted). The
   picture appears once every cell is checked. Approving before that posts
   the text alone and records why, so a picture is never attached after you
   stopped looking. A Revise keeps the verdict of every cell whose row label,
   column and text did not change.
   Under "Claims to verify" each claim shows its verdict, the source link and
   the quoted sentence. Open the link and read the sentence before approving;
   the verdict is a lead, the link is the proof. A contradicted claim blocks
   Approve until the draft is revised or you tick "approve anyway". Every
   revision also hands the drafter each claim that was contradicted (to
   correct or remove, using the fact-checker's note and quote) or could not
   be verified (to soften or drop), so pressing Revise with an empty box
   fixes the failed claims on their own. After a revision a supported claim
   whose wording did not change keeps its verdict; every other verdict is
   discarded and the next `run_verify.py` (or the scheduler) checks the new
   or changed claims.
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
4. Pictures. A draft's chart (part 3) is uploaded and attached to its first
   post with alt text; the dry run prints the file and the alt text. If the
   upload fails nothing is posted and the draft is marked failed, since you
   approved it with the picture. `media: attach_images: false` in
   `publish\config.yaml` posts every draft text-only.

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
  scored that way and a link to the source. For the ones you have an opinion
  about, pick yes (post this) or no and write why; a box with the reason
  categories appears while you type the explanation, which is required.
  This is the same decision `digest.py --rate` asks for at the terminal, and
  it is what the scoring gets tuned against later. It does not change what
  gets drafted today. Use the window links to
  look back 72 hours or a week, or to ignore the score threshold.
- **Publishing** (`/publishing`) — how many drafts are approved and waiting,
  what has gone out, and anything that needs a human (a thread that stopped
  halfway is never retried for you). Read-only: there is no post button here.
- **Feedback** (`/feedback`) — followers over time, your posts ranked by
  impressions, and the suggestions from the latest weekly report. The
  suggestions are proposals only; applying one means editing a settings file.
- **Runs** (`/runs`) — tick the steps you want and press "Run selected
  steps". The page updates as the run goes: the step in progress is shown
  with how long it has been running and its log so far, refreshed every few
  seconds as the step writes, each finished step keeps its final log, and the
  steps still to come are listed. This runs exactly what
  the scheduler in part 6 runs; if the scheduler happens to be running at
  that moment the page says so and does nothing, rather than running twice.
  While a run is going there is a "Stop this run" button: it ends the
  current step (and anything it started, such as the Claude window) and
  skips the rest. Nothing is lost; the next run picks up where it left off.
  The verify step has no time limit (`timeout_seconds: 0` in
  `ops/config.yaml`): a backlog of drafts can take an hour or more, and each
  claim's verdict is saved the moment it lands, so stopping the run keeps
  every claim already checked and only the one in flight is redone next time.
- **Pending / Approved / Snoozed / Rejected / Failed / Voice report** — the
  approval pages from part 3, unchanged.

Links to other websites (a story's source, a DOI, a posted tweet, a
claim check's source, a draft's primary source) open in a new tab, or in your
browser from the desktop window, so the page you were on stays put.

The one thing it writes outside its own pages is a decision on the feed page.
Everything else is a view.

Two things it deliberately will not do. It never posts to X: publishing is
off in `ops\config.yaml` and stays off, and there is no publish button. And
it never edits settings: change `config.yaml` or `draft\voice.md` on disk
(ask me to commit it), not in the browser.

Anyone who can reach the page can run the pipeline, so keep it on
`localhost`. `--host` and `--port` move it and `--reload` is for development;
only use `--host 0.0.0.0` on a network you trust.

### As a desktop app (no browser, no command prompt)

Two levels. The first needs nothing built.

**A window instead of a browser.** Once, `pip install -e ".[desktop]"`. Then:
```
pythonw run_desktop.py
```
`pythonw` (with the w) is the copy of Python that opens no command prompt.
The control panel opens in its own window; close the window to stop it. If
a run is in progress when you close the window, it is stopped too, the
same as the Stop button. It
picks a free port each time so it never clashes with a `run_app.py` you
also have open; add `--port 8000 --host 0.0.0.0` if the phone should reach
the same window. To put it on the desktop, right-click the desktop, New >
Shortcut, and for the location enter (with your own paths):
```
C:\Users\you\X\.venv\Scripts\pythonw.exe C:\Users\you\X\run_desktop.py
```
then set "Start in" to `C:\Users\you\X` in the shortcut's Properties.

**A Pipeline.exe you can double-click.** For a PC without Python set up, or
just to pin it to the taskbar. Build it once per version, from the repo
folder with the venv active:
```
pip install -e ".[desktop]"
pyinstaller deploy\desktop.spec
```
Takes a few minutes. The result is the folder `dist\Pipeline`. Move or copy
the whole folder wherever you like; inside it:
- `Pipeline.exe` is the app. Double-click it.
- `pipeline-cli.exe` is what the runs page uses to run each step. Leave it
  next to `Pipeline.exe`.
- `.env`, `pipeline.db`, `backups\` and `desktop.log` live in that same
  folder (copy your `.env` in before the first start). `desktop.log` is
  where messages go, since there is no command prompt; send it to me if the
  window does not open.
- the settings files (`config.yaml`, `ops\config.yaml`, `draft\voice.md`
  and the others) are under `_internal\`. Editing them there works, but
  they are copies: the next build takes the repo's versions again, so make
  lasting changes in the repo and rebuild.
After every `git pull` that changes code, rebuild; the exe does not update
itself. Everything else is the same as in the browser, including the rule
that publishing stays off unless you turn it on in `ops\config.yaml`.

### Using it from your phone

The page has no login and its buttons run the pipeline, so the phone has to
reach the PC without the PC being open to the internet. Two ways.

**On the same Wi-Fi** (quick, home only). Start the app so it listens on the
network, not just on the PC:
```
python run_app.py --host 0.0.0.0
```
Say yes if Windows asks to let Python through the firewall. Find the PC's
address with `ipconfig` (the "IPv4 Address" line, something like
`192.168.1.23`) and open `http://192.168.1.23:8000` on the phone. This only
works while the phone is on the same Wi-Fi, and anyone else on that Wi-Fi
could open it too.

**From anywhere with Tailscale** (recommended). Tailscale is a free private
network between your own devices; nobody else can reach it.
1. Install Tailscale on the PC and on the phone (tailscale.com) and sign in
   to both with the same account.
2. Start the app with `--host 0.0.0.0` as above.
3. On the phone, open `http://<pc-name>:8000`, using the name Tailscale shows
   for the PC.
Works on mobile data as well as Wi-Fi. The PC still has to be on and running
the app; to have it start by itself, add `python run_app.py --host 0.0.0.0`
as a task in Task Scheduler (part 6) that runs at log-on.

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
| `clinicaltrials_oncology` says `403 Forbidden` | ClinicalTrials.gov blocks a Python program that calls itself a browser. Its entry in `config.yaml` has its own `user_agent` starting with `python-httpx/` for that reason; if the line was removed, put it back |
| A score run logs `safeguards flagged this message` | the CLI's usage-policy check tripped on a batch full of biology abstracts. The scorer does not retry the same prompt; it halves the batch and scores each half in a fresh call, down to single stories. A single story still refused is logged and left for the next run. `scorer_effort` / `drafter_effort` in `config.yaml` set how hard the model thinks (`low` is the cheapest) |
| Want a completely fresh start | delete `pipeline.db`, then `python run_ingest.py --force` |
| The dashboard says `missing env: ANTHROPIC_API_KEY` but you use the Claude Code backend | it should not since the check follows `LLM_BACKEND`; make sure `.env` is in the folder you start `run_app.py` from |
| A step keeps running after you closed the app (a `claude` window keeps reopening) | that was the behaviour before the Stop button; on an old checkout, `taskkill /F /IM pythonw.exe` ends it (or `python.exe` if you started the app from a command prompt) |
| The control panel says a run is already in progress | the scheduler (part 6) is mid-run; wait for it and press the button again |
| The control panel will not start: `Address already in use` | another `run_app.py` or `run_queue.py` window is open; close it or use `--port 8001` |
| `Pipeline.exe` opens and closes at once, or shows a blank window | read `desktop.log` next to it; a missing `.env` or a step that cannot start is logged there. A blank window means the Edge WebView2 runtime is missing: install it from Microsoft (it comes with Windows 11 and most Windows 10) |
| Building the exe fails with `No module named PyInstaller` | `pip install -e ".[desktop]"` in the venv first |

## Changing settings

Everything lives in `config.yaml` (sources, keywords, models, caps; a source
can set its own `min_abstract_chars` when its feed only carries a one-line
summary, as the Fierce Biotech and BioPharma Dive entries do; `linking:` is the
story-linking pass that merges a release with the trade-press write-ups of it
before scoring, `enabled: false` turns it off),
`draft\config.yaml` (how human edits are reused; `images: enabled` draws or
skips the chart), `verify\config.yaml`
(the fact-checking model and the trusted source sites), `publish\config.yaml`
(posting slots, daily post cap, breaking-news rules; `media: attach_images`
attaches or skips the chart), `feedback\config.yaml`
and `ops\config.yaml` (which steps the scheduler runs). Ask me to commit a change rather than editing by
hand, so your copy and GitHub stay in step.
