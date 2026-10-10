# How to use this project, step by step

Windows commands, run from `C:\Users\you\X` in a Command Prompt. On Mac or
Linux the only differences are `python3` for `python`, `cp` for `copy`, and
`cat` for `type`. Every command that talks to Claude runs the Claude Code CLI
with your own login (part 1, step 3); there is no API key to set. Nothing posts
to X until part 5.

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
   NCBI_EMAIL=you@example.com
   ```
   Claude needs nothing in this file: it runs through the Claude Code CLI with
   your own login (next step). An `ANTHROPIC_API_KEY=` line is ignored (it is
   kept away from the CLI, so it can never switch it to pay-per-use API
   billing), and so is an `LLM_BACKEND=` line from an older copy; delete them.
   Optional, free: an NCBI API key on `NCBI_API_KEY=` and your email on
   `CROSSREF_MAILTO=`. Leave the X lines blank until part 5.
3. Install Claude Code and log in (needs Node.js from nodejs.org first).
   ```
   npm install -g @anthropic-ai/claude-code
   claude login
   echo say ok | claude -p --output-format json --model claude-opus-5-5
   ```
   The last line must print `"result":"ok"` inside the output. Every model
   call the pipeline makes (scoring, story linking, the model's yes/no,
   the studio's sessions) runs this CLI as you, so it
   uses your account and anything run on a schedule (part 6) must run as the
   same Windows user. The health checks (parts 6 and 8) have a `cli` line that
   fails when the app cannot find `claude`.
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

## Part 3. Approving posts (start after a few days of part 2)

Every post is written by the studio (part 9): one Opus session per piece,
researched, fact-checked and with designed cards. The older single drafter
(`run_draft.py`), the swarm and its A/B pick page, the claim checker
(`run_verify.py`) and the voice report were retired; the studio is the only writer.
A finished piece lands in the approval queue as a pending draft.

1. Open the approval page.
   ```
   python run_app.py
   ```
   Then open http://localhost:8000/queue in a browser (the front page is the
   dashboard; part 8 explains it). `python run_queue.py` still opens the
   approval page on its own if that is all you want. For each draft: approve,
   edit or reject. An approve is not final: "Reopen" on the approved page
   brings a draft that has not gone out yet back here as pending. Press Ctrl+C
   in the window to stop the server when done.
2. To change a piece in words, use its studio page (part 9): the queue's draft
   page points there. "Edit by hand", folded away on the draft page, is there for
   a one-word fix. A save that breaks a rule (a post over the draft's length limit,
   or an empty thread) is refused on the same page: the reason sits at the top, your
   text stays in the boxes, and nothing is saved or approved. Pick a reason (voice,
   factual, not newsworthy, hard rule, other) when you reject.
3. The piece's cards are shown under "Image" with the exact text a screen
   reader will get (the alt text), each attached to the post it is anchored to
   when published. "Drop image" posts the text alone; with two or more pictures
   the button reads "Drop both images" and each picture also has its own "Drop
   this picture" beside it (`POST /drafts/{id}/image/{index}/drop`), which removes
   that one and leaves the others in place.
4. While the studio is working on a piece (a running stage, or a request you
   made waiting), its draft is "on hold": approve, edit, reject and the picture
   drops are refused until the session is done.

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

Posting is **manual only**. A draft goes out when you press "Publish now" next
to it on the panel's approved page (part 8), or when you run the publisher
yourself with `--live`. Nothing posts on a timer: the panel has no automatic
publisher, and the scheduler (part 6) refuses to run if any step in
`ops\config.yaml` carries `--live`. So you choose the moment each post goes
out; pick a time when you can stay with it for the first hour and answer
replies, since that hour decides how far X shows it.

**Copy-paste posting (shipped, no X API needed).** With `posting: manual` in
`publish\config.yaml`, "Publish now" opens a copy-paste page instead of calling
the X API:

- each post of the thread with a **Copy text** button (numbered exactly as the
  publisher would post it), and under it its picture(s) with **Copy picture**,
  a download link and the alt text for X's "Add description";
- for a studio piece (part 9), above the posts, the fast-moving facts its
  session flagged to **re-check before posting** (a date that may have moved, a
  figure that changes daily): confirm each still holds before you paste;
- a link to x.com's composer. Paste post 1's text and picture, press **+** for
  each next post, and post it;
- back on the page, optionally paste the link to the first post (feedback can
  then read its metrics), and press **I posted it, everything went OK**. That
  logs the draft as posted, so it leaves the approved list and counts toward
  the daily limit. Nothing is sent to X from here.

Set `posting: api` to go back to posting through the X API (the steps below).

1. Rehearse. With no flags nothing is sent; it prints what would post and
   when, based on approved drafts and the slots in `publish\config.yaml`.
   ```
   python run_publish.py
   ```
   The shipped caps are `max_posts_per_day: 50` and `min_gap_minutes: 5`; they
   are a safety limit on every post, "Publish now" included. The cap is a
   ceiling, not a target: many posts a day nobody answers teach the ranker to
   skip the account. The shipped `slots: []` means a hand-run
   `run_publish.py --live` (without `--now`) posts the top approved draft as
   soon as the gap and the daily cap allow; list times under `slots:` (e.g.
   `"08:30"`) to have it post only inside those windows. "Publish now" ignores
   slots.
2. Go live. Two things are required, so nothing posts by accident: in `.env`
   ```
   PUBLISH_ENABLED=1
   ```
   and the `--live` flag, which only you ever pass (the panel's "Publish now"
   passes it for the one draft you pressed it on):
   ```
   python run_publish.py --live --now --draft 17
   ```
3. Useful variants, all run by hand.
   ```
   python run_publish.py --live --now        # ignore slots, post the top candidate once
   python run_publish.py --live --breaking   # only FDA / company-approval items
   python run_publish.py --live --limit 1    # at most one post this run
   python run_publish.py --live --now --draft 17  # post draft 17 now (what the panel's "Publish now" runs)
   ```
   A thread's replies are numbered " (2/4)", " (3/4)" and so on; its first post goes out
   exactly as approved, with no " (1/4)", because the opening post carries no
   position marker and the first post is the one X shows
   people who do not follow the account (`thread_numbering` in
   `publish\config.yaml`: `replies`, `all` or `none`).
   Every draft is a thread and is posted as one. When several drafts wait for one
   slot, the order you set on the panel's approved page (part 8) goes first;
   drafts you did not number follow it by the policy in `publish\config.yaml`.
   A draft is posted at most once even if the command runs twice. A thread
   that fails part-way is marked partial and left for you; it is never
   retried automatically.
   A draft whose attempt failed before anything went out (a dropped
   connection, an image upload error) goes back to the approved list on its
   own and the next run tries it again, up to three failed attempts
   (`retry` in `publish\config.yaml`). After that, or for a text the checks
   refused, it stays put until you fix the cause and release it:
   ```
   python run_publish.py --release-failed        # every failed or refused draft
   python run_publish.py --release-failed 17     # just draft 17
   ```
   This posts nothing and never touches a posted or partial draft. The panel's
   approved page (part 8) has the same thing as a button: "Release". If a
   publish run is killed part-way — the PC sleeps, the panel's Stop button, a
   power cut — the draft it had taken can be left marked `claimed`, and a
   claimed draft is skipped by every later run. That button is also what frees
   one of those, once the claim is more than 30 minutes old (younger than that
   a run may still be posting it, and the button says so).
4. Pictures. A piece's cards (part 9) are uploaded and attached to the posts
   they are anchored to, with alt text; the dry run prints the file and the alt text. If the
   upload fails nothing is posted and the draft is marked failed, since you
   approved it with the picture. `media: attach_images: false` in
   `publish\config.yaml` posts every draft text-only.

---

## Part 6. Running it on a schedule (Windows Task Scheduler)

You may not need this part. While the control panel or desktop window is open it
already runs ingest, score, studio_scan, studio, feedback and studio_learn on
its own at 01:00, 03:00, 06:00, 09:32, 12:00 and 15:00 (part 8, "Automatic runs"), and it never publishes. Task
Scheduler is the other way: it runs even with the app closed and can wake the
PC, but it needs the tasks below. Pick one for `pipeline-run` and
`pipeline-studio`. Running both is
safe (a step that is already running is skipped, never run twice) but wasteful.
The automatic runs also back the database up once a day (the first run each
day), and each one records a health check and alerts when one fails, but they
do not check health on the hour while nothing runs; keep the `pipeline-health`
task from step 2 if you want that. `pipeline-backup` is not needed with them. The health limits in
`ops\config.yaml` are sized for three runs a day; if `pipeline-run` runs every
30 minutes, set `max_hours_since_ingest` back to 3, `max_hours_since_score` to 6
and `source_stale_min_hours` to 0 for earlier warnings.

1. Try the orchestrator by hand first. It runs ingest, score, studio_scan,
   publish (a dry run), feedback and studio_learn in order and records each step. It leaves the `studio` step out:
   one studio session runs for an hour or more, and the whole run (and every
   `pipeline-run` after it, which Task Scheduler does not start while one is
   still going) would wait for it. The studio has a task of its own,
   `pipeline-studio` in step 2, which runs `run_ops.py run --only studio` and
   holds only the studio's own lock, so the pipeline runs on beside it.
   Each step takes its own lock
   (`<lock_path>.<lock name>`) while it runs, so a step the control panel is
   already running is skipped as `locked` and the rest carry on.
   Until that settles, run the steps by hand from the panel and post from
   the approved page; the scheduler can come back later.
   ```
   mkdir logs
   python run_ops.py run --dry-run
   python run_ops.py run
   python run_ops.py run --only studio --dry-run
   python run_ops.py status
   python run_ops.py health
   python run_ops.py backup
   ```
2. Open a Command Prompt as Administrator and create the tasks:
   ```
   schtasks /Create /TN "pipeline-run" /SC MINUTE /MO 30 /TR "cmd /c cd /d C:\Users\you\X && python run_ops.py run >> logs\ops.log 2>&1"
   schtasks /Create /TN "pipeline-health" /SC HOURLY /TR "cmd /c cd /d C:\Users\you\X && python run_ops.py health --alert >> logs\ops.log 2>&1"
   schtasks /Create /TN "pipeline-backup" /SC DAILY /ST 03:00 /TR "cmd /c cd /d C:\Users\you\X && python run_ops.py backup >> logs\ops.log 2>&1"
   schtasks /Create /TN "pipeline-studio" /SC DAILY /ST 06:10 /RI 360 /DU 12:30 /TR "cmd /c cd /d C:\Users\you\X && python run_ops.py run --only studio >> logs\ops.log 2>&1"
   ```
   `pipeline-studio` runs at 06:10, 12:10 and 18:10; it starts at most one
   piece a day (part 9) and otherwise only acts on what you asked for on the
   studio page. Each backup also keeps a copy of your studio playbook
   (`backups\pipeline-<stamp>.studio_playbook.md`, `with_db` under `backups:`
   in `ops\config.yaml`). In the Task Scheduler app, open each task and tick "Run whether user is
   logged on or not" and "Wake the computer to run this task". The PC must be
   on for them to fire. Keep each task running as your own Windows user, the
   one that ran `claude login` in part 1: every model call uses that login.
3. The scheduler runs the `publish` step as a dry run: it lists what it would
   post and posts nothing, and it stays that way. Posting is manual only (part
   5): `run_ops.py run` refuses to start if any step in `ops\config.yaml`
   carries `--live`, so press "Publish now" on the approved page when you want
   a draft to go out.
   The `feedback` step is on in `ops\config.yaml`: the account
   has the paid X API read tier, so put the bearer token on `X_BEARER_TOKEN=`
   in `.env` (Part 7) and every scheduled run also snapshots metrics. Without
   the token the feedback step fails with an error in the log and the run
   carries on (it is optional).
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
   want and I will commit them. The studio learns from the same snapshots on its
   own (part 9, "What X says"); without this tier, type its numbers in there.
   Studio posts show as their own `studio` group in the tables. The feed and
   voice-guide proposals compare the retired drafter's posts only, so with the
   studio as the only writer they have nothing new to work from (the voice guide
   they name, `draft\voice.md`, was removed with the drafter).

   Every table in it is ranked by one KPI, `kpi:` in `feedback\config.yaml`.
   The shipped value is `conversation`: not a number X reports, but a weighted
   sum of the ones it does (a reply or a quote counts 3, a bookmark or a repost
   2, a like 1, an impression 0.05, so 20 impressions equal one like). The
   engagement counts are the signals the ranker pays for; impressions are in at
   a small weight because a small account's posts mostly get no engagement, and
   without them nearly every post scores 0.
   Set `kpi: impressions` (or `likes`,
   `replies`, ...) to measure a raw count instead.
---

## Part 8. The control panel (one window for everything)

```
python run_app.py
```
Open http://localhost:8000. Leave it running in its own window; press Ctrl+C
to stop it. The panel is dark, in the style of a 1980s synthwave poster: a
striped sun behind the menu bar, a neon grid at the bottom of the window, and
every page's content on one dark panel so nothing runs behind the text. Status
pills differ in shape as well as colour (a solid red fail, an outlined amber
warn, a dashed skip), and the buttons that post to X ("Publish now", "I posted
it") are the only ones filled with the sunset colours. Nothing on the page
moves except the progress bar of a long action; with your system set to reduce
motion, that bar stands still too. Four pages:

- **Dashboard** (`/`) — the same health checks `python run_ops.py health`
  prints, worst first (`cli` says whether the app can find the Claude Code
  CLI every model call runs through), plus the last outcome of every
  scheduled step, how many rows are in each table, the database size, free
  disk and the age of the latest backup. This is the page to open when
  something looks wrong.
  "Back up now" under Storage saves a copy of the database right away, the
  same verified copy `run_ops.py backup` makes, into the same `backups\`
  folder, with your studio playbook beside it (the oldest beyond
  `backups: keep` in `ops\config.yaml` are removed, playbook copies too).
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
  it is what the scoring gets tuned against later. Use the window links to
  look back 72 hours or a week, to show 10, 25 or 100 stories (100 is the
  most the page lists), or to ignore the score threshold. "Hide decided"
  drops the stories you have already answered, so what is left is your
  to-do list; a decided story otherwise stays until it ages out of the
  window. "Show decided" brings them back. The "Ingest and score" button at
  the top runs those two steps (the same run the runs page starts); the
  page says a run is going and the log is on the runs page.
- **Pending** (`/queue`) — the approval queue from part 3.
- **Approved** (`/status/approved`) — the drafts waiting for a slot, and
  where you decide what posts when. "Publish now" next to a draft posts that
  one draft right away, outside the slots (the daily cap and the minimum gap
  from `publish\config.yaml` still apply, and the run's log is on the runs
  page). It only posts when `PUBLISH_ENABLED=1` is in `.env` (part 5);
  otherwise the page says so and the button only rehearses. "Set schedule"
  shows a number box next to each waiting draft: number them 1, 2, 3 for the
  order the scheduled slots should post them and press "Save order". A
  draft without a number follows the numbered ones by score. The order is
  kept on the draft (shown as #1, #2) until it posts. "Reopen" next to a
  waiting draft takes it back off the list and makes it pending again, so you
  can edit or reject it; the order you saved for it is forgotten
  rather than coming back the next time you approve it, and the box beside the
  button puts your reason in the draft's history. It is refused for anything
  already on X — posted, or a thread that stopped halfway — and for a draft a
  publish run has just claimed, because that run will not look at the draft
  again before it posts (the draft's own page says which); reopening cannot
  unpost a tweet, so reject it instead if it should not run again. A draft
  whose last attempt failed or was refused can be reopened, and its failed
  schedule row goes with it, though the posts log keeps the history.
  "Release" next to a draft is the other half of that: it puts a draft whose
  publish attempt posted nothing back in line *without* taking it off the
  approved list, so the next run tries it again with the order you set kept.
  It is offered when the attempt failed or was refused, and for a draft left
  marked `claimed` by a run that died before it posted (a claim is releasable
  only once it is more than 30 minutes old; younger than that a run may still
  be posting the thread, and the button is not offered). It is refused for
  anything already on X, posted or half-posted, since releasing that would
  post the thread a second time. Both buttons are on the draft's own page too.
- **Publishing** (`/publishing`) — how many drafts are approved and waiting,
  what has gone out, and anything that needs a human (a thread that stopped
  halfway is never retried for you). Posting is manual only: a draft goes out
  when you press "Publish now" next to it on the approved page, and at no
  other time; there is no automatic publishing. The two limits at the top,
  posts per day and the minimum gap between posts in minutes, are the one
  setting the panel edits here ("Save limits" writes them into
  `publish\config.yaml`, and the next "Publish now" uses them).
- **Studio** (`/studio`) — part 9: every studio piece with its stage, the box
  to write a piece on any topic, the queued topics, and the playbook every
  session reads. A piece's page shows its fact base, post, cards, fact-check log
  and live session log, with Continue (after research), Revise and Resume.
  **What X says** (`/studio/performance`) shows each posted piece's numbers
  against the account's median, what each angle, shape, hook style, card
  count and voice has done and how often the lean suggests it (a voice: how often
  it is drawn), what you did with each voice's pieces in the queue, and the
  playbook's history, with the forms to type numbers in, add a hand-posted post's
  link, apply a proposed playbook or put an old one back.
- **Radar** (`/studio/radar`) — part 9: where topics come from. The day's scan
  topics, the feed's top stories and the catalyst calendar, each with **Write
  it**, and the **Scan now** button.
- **Feedback** (`/feedback`) — followers over time, your posts ranked by
  impressions, and the suggestions from the latest weekly report. The
  suggestions are proposals only; applying one means editing a settings file.
- **Runs** (`/runs`) — every run's log, whichever page started it: the
  feed's "Ingest and score", the studio's buttons, the approved page's "Publish now", or the checkboxes here (tick the steps you
  want and press "Run selected steps"). The page updates as the run goes: the step in progress is shown
  with how long it has been running and its log so far, refreshed every few
  seconds as the step writes, each finished step keeps its final log, and the
  steps still to come are listed. Runs of different steps go at the same
  time: start an ingest run while a studio session works, or open a second desktop
  window and run something there. The one thing that never happens is the
  same step running twice. Its button is greyed out while it runs in this
  window, and a step another window or the scheduler (part 6) is already
  running is skipped and marked `locked` in the log instead of running twice.
  Ingest and score count as one step for this (they share the `stories` lock in
  `ops/config.yaml`), because scoring merges stories that ingest may still be
  adding articles to. "Publish now" is one publish at a time. Each run's "Stop
  this run" button stops only that run. This runs exactly what the scheduler
  in part 6 runs.
  While a run is going there is a "Stop this run" button: it ends the
  current step (and anything it started, such as the Claude window) and
  skips the rest. Nothing is lost; the next run picks up where it left off.
- **Automatic runs** (top of `/runs`) — while the panel or the desktop window
  is open it runs ingest, score, studio_scan, studio, feedback and studio_learn on its own,
  at 01:00, 03:00, 06:00, 09:32, 12:00 and 15:00 local time as shipped. Publishing is never one of
  them: posting stays the approved page's "Publish now", and an automatic run
  cannot post even if `.env` allows posting. The box says whether it is on,
  when the next run is, and what happened at each recent time (started, or
  skipped and why); automatic runs are marked `automatic` in the list below,
  and the dashboard shows the next one and how the last one ended. Tick or
  untick "On" and edit the times (HH:MM, at most 8 a day, at least an hour
  apart), then Save; it takes effect within half a minute. That writes two
  lines of `ops\config.yaml` (`auto_run_enabled` and `auto_run_times`) and
  nothing else, so `git status` will show that file as changed. Which steps run
  is `auto_run_steps` in the same file (edit it by hand, then restart the app).
  How it behaves:
  - It only runs while the app is open. A time that passed while the app was
    closed is not made up: press the run buttons if you want a run then.
  - A time missed while the computer slept runs when it wakes, if it is less
    than an hour late (`auto_run_grace_minutes`); several missed at once run
    once.
  - If a step from the last run (or one you started) is still going, the next
    time waits for it, up to that same hour, and is otherwise skipped with the
    reason shown.
  - Switching it on, or adding a time that is already past today, never starts
    a run straight away; the next time on the clock does.
  - With two windows open, only one of them runs the timer; the other says so.
  - The first automatic run each day also backs up the database, the same
    verified copy as the dashboard's "Back up now", into `backups\` (the
    oldest past `backups.keep` are removed). It happens when a run starts and
    the newest backup is more than 20 hours old (`auto_run_backup_hours` in
    `ops\config.yaml`; 0 turns it off), so if the app was closed at 06:00 the
    next run that day does it. The box lists "backup saved" or "backup failed"
    with the reason; a failed backup never holds the run back, and the
    dashboard's backup age turns red if backups stop.
  - When an automatic run finishes it records a health check and, if a check
    is failing, sends the alert from part 6 (webhook or email), since nobody
    was watching it.
  - Each automatic run costs what pressing the buttons costs.
- **Pending / Approved / Rejected / Failed** — the
  approval pages from part 3. The Approved page is the waiting list for
  `run_publish.py`: each row says `waiting`, `posted` (a link to the tweet),
  `failed` or `partial thread`, and drafts already posted are hidden until you
  press "Show posted". The draft page shows the same, with the posting time.

Links to other websites (a story's source, a DOI, a posted tweet, a
draft's primary source) open in a new tab, or in your
browser from the desktop window, so the page you were on stays put.

The one thing it writes outside its own pages is a decision on the feed page.
Everything else is a view.

Two things it deliberately will not do. It never posts to X on its own:
posting is the approved page's "Publish now" and nothing else, and the
automatic runs never include it. And it edits only a few settings: the posting
caps on `/publishing`, the automatic runs' switch and times on `/runs`, and the
studio's playbook. Change `config.yaml` or `studio\brief\voice.md`
on disk (ask me to commit it), not in the browser.

Anyone who can reach the page can run the pipeline, so keep it on
`localhost`. `--host` and `--port` move it and `--reload` is for development;
only use `--host 0.0.0.0` on a network you trust. Other websites open in the
same browser cannot press its buttons: a button press (or form) that comes
from any page but the app's own is refused with "Refused: this request was
sent from another site" and changes nothing. If you see that message after
pressing a button on the app itself, you reached it through something that
renames it on the way (a proxy); open it by its own address instead.

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
- the settings files (`config.yaml`, `ops\config.yaml`, `studio\config.yaml`
  and the others) are under `_internal\`. Editing them there
  works, but they are copies: the next build takes the repo's versions again,
  so make lasting changes in the repo and rebuild.

**Where the data lives.** The exe keeps its data next to itself, NOT in the
repo: `.env`, `pipeline.db`, `backups\`, `images\`, `studio_pieces\` (every
studio piece: its fact base, posts, cards, fact-check log and session),
`studio_playbook.md` (your studio playbook, once you have saved it) and
`desktop.log` all sit in the folder that holds `Pipeline.exe`. Each backup keeps
a copy of the playbook beside the database's; `studio_pieces\` is in no backup,
so copy it yourself when you move the app. Copy your `.env` in before the first
start (and `pipeline.db` too if you want the stories and drafts you already
have from running the scripts in the repo folder; otherwise the app starts
with an empty database). `desktop.log` is where messages go, since there is
no command prompt; send it to me if the window does not open.

**Rebuilding.** After every `git pull` that changes code, or after a
settings change in the repo, rebuild; the exe does not update itself. Close
`Pipeline.exe` first. PyInstaller asks
`The output directory "...\dist\Pipeline" and ALL ITS CONTENTS will be
REMOVED! Continue?` and means it: if you have been running the app from
`dist\Pipeline`, its `.env`, `pipeline.db`, `backups\`, `images\`,
`studio_pieces\` and `studio_playbook.md` go with it. A studio piece whose
folder is gone can no longer be resumed or revised (its page says the folder
is missing), and the studio falls back to the shipped playbook. Two ways to
keep them:
- keep the app outside `dist\` (say `C:\Users\you\Pipeline`): answer `y`,
  then copy `Pipeline.exe`, `pipeline-cli.exe` and `_internal\` from
  `dist\Pipeline` over the top of that folder. Its data files are never in
  the way. This is the simplest habit.
- or run it from `dist\Pipeline`: answer `N`, copy `.env`, `pipeline.db`,
  `backups\`, `images\`, `studio_pieces\` and `studio_playbook.md` somewhere
  safe, rebuild with `y`, and copy them back. If the copy says `The system
  cannot find the file specified`, that file was never there and there is
  nothing to keep.
Everything else is the same as in the browser, including the rule
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

## Part 9. The studio: one Opus session per post

The studio is how the account's best posts get made now. Each piece is ONE Claude
Code session on Opus 5.5 at max effort, the same way the Merck SPR2015, Summit
catalyst map and next-gen CTLA-4 posts were made by hand in a chat. The session
researches the topic and writes a fact base with every source. It then writes a
long post (or a thread of long posts), designs the cards, and runs its own cold
fact-check through a fresh sub-agent. The app draws the cards, counts characters
the way X does, checks the few lines that are never crossed (no investment or
medical advice, no links) and hands any problem back to the same session. The
finished piece waits in the pending queue as "Studio piece N" with its cards
attached. Cards are checked too: text cut off, overlapping or off the edge, and big
empty areas, all handed back to the session to fix. Nothing posts from here: approving and "Publish now" work as in part 5,
and long posts need X Premium.

1. Nothing new to install. The session runs through the Claude Code you set up
   in part 1, and the cards are drawn by Microsoft Edge, which comes with
   Windows (Chrome or Chromium work too). If a piece says `no Chromium-family
   browser found`, put the browser's full path on `render: browser:` in
   `studio\config.yaml`, between the single quotes that are there:
   `browser: 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'`
   (never double quotes: they turn each `\` into an escape and the studio stops
   loading its settings), or on `STUDIO_BROWSER=` in `.env`. Until a browser is
   found no automatic piece starts: the `studio` step fails on the runs page with
   `no new piece: no browser to draw the cards with`, rather than spend a
   session's research and writing on a piece whose cards cannot be drawn.
2. Up to three pieces are written at once (`max_parallel: 3` in
   `studio\config.yaml`, with `slots: 3` on the studio steps in
   `ops\config.yaml`; change both together). Each press of "Write now" on a
   queued topic, or a Continue, Revise or Resume, starts its own run in a free
   writing slot, so queued topics no longer wait for the piece before them; a
   fourth press while three run is refused until one ends. Each run is a full
   Opus session on your one Claude login, so three at once use your plan's
   limits three times as fast; set `max_parallel: 1` for the old one-at-a-time.
   It runs on its own too. The `studio` step sits in the automatic runs (part 8)
   right after `score` (with Task Scheduler instead, it is the `pipeline-studio`
   task of part 6), so each run first acts on anything you asked for on the
   studio page, then starts a new piece if `studio\config.yaml` allows one: at
   most `max_new_per_day` (1) a day, `min_hours_between` (6) hours after the last
   piece, never while a piece waits for you at the research checkpoint, and never
   while `max_waiting` (2) studio pieces wait unread in the queue (a piece written
   while the last ones wait goes stale; 0 turns the limit off). A day
   is the calendar date in the `timezone:` of the root `config.yaml`, the clock the
   run times are set in, so the day's first run that may start a piece does,
   whatever minute yesterday's started at. An automatic piece picks its own story
   from the top scored stories of the last two days that the account has not
   written about (no studio piece on it and no draft that did not fail), or finds a
   better one with its own news scan (only when nothing on offer makes a strong
   piece: the radar and the feeds have read the news already), and writes straight through (`auto:
   checkpoint: false`). It is also offered the radar (item 10): the day's scan
   topics and the catalysts coming up or just passed. It is told every piece of
   the last 10 days (`topics: avoid_days` in `studio\config.yaml`), finished or
   still waiting for you, so it does not repeat a topic. The piece is tied to a
   feed story only when it is one the app offered it; a number it made up ties
   it to nothing. Expect
   it in the queue 30 to 90 minutes after the run starts. A
   session still running at the next run time sits that run out
   (`skip_when_busy`) instead of holding the other steps back, and so do the
   steps still waiting behind it in its own run
   (feedback, studio_learn): that run gets to them when the session
   ends, and the new run says so in its note on the runs page.
3. Or start one yourself. Open **Studio** in the panel's top bar, type a topic
   ("next-gen CTLA-4", "Merck's KRAS G12D deal", "what ESMO week means for
   $SMMT"), pick an angle or leave it to the session, and press **Write it**. On
   the **Feed** page, **Write a studio piece** under a story does the same with
   that story. A piece you start stops after research by default (the tick box)
   so you can read the fact base first. If the feed merges that story with another
   before the studio gets to it, the piece follows it to the merged story. The run
   shows on the runs page like any other; its Stop button ends it, and the piece
   shows `interrupted` with Resume and Discard as soon as the run has stopped.
4. The research checkpoint. When a piece says **read the research**, open it:
   the fact base is there with every source marked opened or seen in search
   results only, the X handles it verified, the corrections it made and the
   questions it could not answer. Type any direction (the angle you want, what to
   lead with, what to drop) and press **Continue: write the piece**.
5. Review. The piece's page shows the post exactly as it will be posted, the
   cards, the fact-check log (every finding, what changed, what stayed
   unverified) and the session's own log. The same draft is on the pending page;
   approve it there. Once you change the text by hand in the queue, the piece's
   page shows the queue's text as the post, with the session's own files folded
   away below it. A piece cites an analyst's price target only when it bears on
   the story, and then the post says what the target rests on (the firm's
   assumptions: which indications it values, peak sales, the odds, the timing),
   whether the catalysts it tells readers to watch are in it and, if not, which
   assumption each would move and so which way the target would go, in words. It
   never gives a target of its own, nor a new number for an analyst's. The app
   sends a piece back while a target it cites has no entry in piece.json, or an
   entry whose quoted words (`post_says`) are not in the post; what is still left
   after the last polish round reaches you as a warning on the draft. A post or
   card that states a target, fair value or value per share of the account's own
   stops the piece: the checks catch the usual wordings, and the cold fact-check is
   told to flag any target figure no source published. So read what the post says
   about each target before you approve. The piece's page lists the targets it
   cites, and the copy-paste page asks you to check they are still each firm's
   latest before posting.
6. Changes. Type them on the piece's page and press **Revise**: the session that
   wrote it rewrites it, fact-checks what changed and replaces the queue draft.
   A draft you rejected comes back to pending with the revision; one you already
   approved has to be reopened on the approved page first (the piece says
   `not revised: draft N is approved ...` otherwise, and no session is spent).
   The queue's own Revise button would flatten a studio piece, so it points you
   here instead. Hand edits on the queue page still work, and a Revise keeps
   them: before the session runs, the app writes the queue's text into the
   piece's post files and tells the session the editor changed it by hand and
   the changes stay; a card you dropped in the queue is taken out of the piece
   and stays out unless your note asks for it back. Keep each section apart with
   a blank line, since a line of only `---` splits a post there. A rejected
   draft comes back only if none of it reached X (a posted one would never be
   posted again, so no session is spent on it), and without the publishing
   order it had before. While the studio works on a piece, or a run you asked
   for waits to start, its queue draft is **on hold**: Approve, Edit, Reject and
   Drop picture say so instead of acting, since the revision replaces the draft
   when it lands.
7. When something stops. A piece marked `interrupted` (stopped, timed out, the
   PC slept, Claude Code could not start, or Claude Code stopped the cold
   fact-check before it reported) or `failed` (the checker still found a
   blocking problem after the polish rounds, or a card could not be copied into
   the queue because the disk was full or another program held the picture open)
   has **Resume where it stopped**: the
   same session carries on with everything it already read, and anything you type
   in the box goes to it. If you edited its queue draft by hand while it was
   stopped, Resume goes through a revision first, so your edit is not
   overwritten; if you approved the draft a stopped revision would replace,
   Resume says so and runs nothing until you reopen it. A piece whose
   run was stopped (the Stop button, the desktop window closed, the PC restarted)
   turns `interrupted` as soon as you open its page or the studio page, or the
   panel sees the run end; while a studio run is still going, its pieces keep
   their stage. Claude Code deletes sessions it has not used for 30 days, so a
   piece resumed or revised after that is picked up by a fresh session that
   reads the piece's own files (fact base, posts, cards, fact-check log) first;
   the piece's session log says when that happened. **Discard** gives up on a
   piece: its files stay in the `studio_pieces` folder, and a draft of it still
   pending in the queue is
   rejected. A piece discarded before it reached the queue gives its story back to
   the shortlist.
8. The playbook. **Studio → The playbook** is the short note every session
   reads when it researches and when it writes: what works on this account and
   the mistakes the fact-checks keep catching, and, in its **Voices** section,
   the voices the pieces are written in (below). Edit and save it (the box under it
   says what you changed, for the history); the next piece written reads your
   version, even one whose run is already going (it lives in `studio_playbook.md`
   next to `pipeline.db`, and every backup keeps a copy:
   `backups\pipeline-<stamp>.studio_playbook.md`; to restore one, copy it back
   as `studio_playbook.md`, or put a version back on the performance page). Every
   version is kept: yours, the learning loop's (below) and the shipped seed, and
   any one can be put back from the performance page. The session is also given
   the X handles in `config.yaml` as already verified, and a copy of the
   account's recent pieces as they stand in the queue (`earlier_pieces.md` in the
   piece's folder, each marked posted or not), so a follow-up or a scorecard
   quotes what the account actually wrote.
   **Where the usage goes**, at the foot of the performance page, adds up every
   studio run of the last 30 days by stage (research, write, polish, revise) and by
   where the piece ended up (posted, approved, waiting, rejected, stopped before
   the queue), in what the CLI reports each run would cost on the API: nothing is
   billed on your Claude login, but it shows which part uses the plan's limits.
   To run a stage at a lower effort than the writer's (`effort: max`), set it
   under `stage_effort:` in `studio\config.yaml` (blank, as shipped, keeps max);
   `reference_stage: write` has the session read the reference pieces when it
   writes rather than through all of research. To compare before you change
   either, write one topic each way and judge the drafts blind:
   `python run_studio.py --topic "..." --no-checkpoint --trial current`, then the
   same with `--trial polish` (polish at high effort) and `--trial all` (that plus
   the references at the write stage). A piece's studio page says which trial it
   was under "usage trial: reveal its setup".
9. What X says. **Studio → what X says** (`/studio/performance`) is the
   dashboard for the posted pieces, and what it shows is fed back into the next
   session. Each piece is measured on its first post 48 hours after it went out
   (`learn: horizon_hours`), on the same conversation score as part 7 (replies
   and quotes count 3, bookmarks and reposts 2, likes 1, impressions 0.05), and
   compared with the median of every post the account made in the 30 days
   before it. From those scores:
   - every new session's brief gets a **WHAT X SAYS** section: how many pieces
     are measured, which angles, shapes, hook styles and card counts did best
     and worst (with how many pieces each), and the openings of the best and
     weakest posts, with an honest "too few to be sure" while there are under
     twenty;
   - it is offered a **lean**: an angle, shape and hook style to favour if the
     story supports it. The lean is a weighted draw, so what has done better is
     suggested more often and what has little evidence still gets tried; it only
     picks among what the variety rules leave on offer, and the session still
     chooses (the page shows what each piece was offered and whether it went
     with it);
   - after every 3 newly scored pieces (at most once a day) one call on the
     writer's model proposes a rewrite of **the playbook** from the evidence and
     your hand edits on the queue page. It waits on the page, with its changelog
     and what it would change, until you press **Apply this version**
     (`learn: playbook: propose`, shipped: a handful of posts is mostly the story
     and the day, and the numbers mean little before 20 to 30 pieces). `auto`
     applies each rewrite at once, `off` never rewrites. **Put this version
     back** restores any earlier version, and **Rewrite the playbook now** runs
     a rewrite whatever the counts. A rewrite that fails or comes back unusable
     changes nothing.
   The numbers come from the feedback step's snapshots, which need the paid X
   API read tier (part 7). Without it, open **type the numbers X shows** under a
   piece about 48 hours after posting and copy them from the post's analytics on
   x.com; typed numbers count as they are. A piece you posted by hand without
   giving its link has **add the post's link**: paste it and the feedback step
   measures it from then on. Nothing on this page posts anything.
   **Voices.** So the account does not always sound the same, every piece is
   written in one of the voices listed under **## Voices** in the playbook. Four
   ship: the desk note (terse, the number first), the explainer (warm, curious,
   thinks out loud), the sceptic (reads the footnotes, says what doesn't add up)
   and the storyteller (starts from a turn in the story). The app gives each new
   piece one at random, never the voice of the piece before, and the session never
   chooses: the voice is named in the writing stage's instructions (research is told
   too, so it gathers what that voice needs), shown on the piece's page and kept
   through every revision. Every voice is the same person speaking in the first
   person, with real reactions ("I couldn't believe the data", "this deal doesn't
   make any sense to me", "I wonder why they didn't include another dose"): the
   voice guide asks for them, and the checker sends a piece back to the session
   when too few of its sentences speak as "I", "me" or "my" for its length (at
   least one per 1,200 characters, never more than six asked for; the
   `first_person_` settings under `voices:` in `studio\config.yaml`). The
   performance page shows, per voice, what X says and what you did with its pieces
   in the queue: how many you changed by hand and how much of the words, the
   revisions you asked for and the rejections. That second table says something
   from the first week; X takes months to tell four voices apart. Until 30 scored
   pieces carry a voice (`voices: lean_min_measured`) the draw stays even; after
   that it follows the numbers, as the lean does. The playbook rewrite reads both
   tables and may propose a new voice, a sharper one or the end of one; any such
   change waits for you to apply it, even with `learn: playbook: auto`. To change
   the voices yourself, edit the section: each voice is a heading
   `### key: Name` and a few sentences, a new voice gets a new key (the numbers
   are kept by key), and a heading with no voice under it turns voices off
   (`voices: enabled: false` in `studio\config.yaml` does too).
10. The radar. **Radar** in the top bar (`/studio/radar`) is where topics come
   from, and the same list is offered to every automatic piece:
   - **Topics from the scan.** Once a day, before the first automatic studio run,
     one call on the writer's model (Opus 5.5 at max, with web search) reads the
     last few days of immuno-oncology news and proposes 3 to 5 topics. Each says
     why now and names its companies, its sources and a suggested angle. **Write
     it** queues the topic, with the scan's reasons and sources, and the angle you
     leave selected ("the session chooses" works too), then starts the studio.
     **Dismiss** takes a topic off. **Scan now** runs a scan at once.
   - **From the feeds.** The top scored stories the account has not written
     about (no piece and no draft), each with **Write it**.
   - **The catalyst calendar.** Dated events: PDUFA dates, readouts, conference
     presentations, advisory committees. Each scan reports the ones it finds, and
     every piece's research adds the ones its session confirmed, with the source.
     A date is as precise as its source (a day, a month, a quarter). **Write the
     preview** queues a piece on an event coming up, and **Write the reaction**
     one on an event that just passed, each with a fitting angle selected.
     **Dismiss** takes an event off (for instance an old date after it moved).
   - **May repeat.** A topic or a catalyst that looks like a piece started in the
     last `radar: repeat_days` days (30), or a topic still in the queue, carries a
     **may repeat** flag naming that piece, its stage and angle, and what they share:
     a drug or trial (a generic name, a code like AZD0486, KEYNOTE-B15, an NCT
     number), a source URL, or a company plus at least two words of the title. Its
     button reads **Write it anyway**: the flag never blocks, since a story with
     real news is worth a second piece. An automatic piece sees the same flag on
     that topic in its brief and is told to take it only with news the earlier
     piece did not have. `repeat_days: 0` turns the flag off.
   A failed scan changes nothing: the page says why, and the next run tries
   again. The settings are `radar:` in `studio\config.yaml`.
11. From a command prompt (the same thing the buttons do):
   ```
   python run_studio.py --list
   python run_studio.py --topic "next-gen CTLA-4" --checkpoint
   python run_studio.py --story 123 --angle readout_reaction
   python run_studio.py --now --no-checkpoint
   python run_studio.py --resume-only
   python run_studio.py --dry-run
   python run_studio.py
   python run_studio.py --scan
   python run_studio.py --scan-now
   python run_studio.py --scan --dry-run
   python run_studio.py --learn
   python run_studio.py --learn-now
   python run_studio.py --learn --dry-run
   ```
   `--list` shows recent pieces and queued topics; `--dry-run` prints the
   research prompt the next piece would get and starts nothing; plain
   `run_studio.py` is the automatic run. `--scan` runs the radar's scan when one
   is due (once a day), `--scan-now` runs it now, and `--scan --dry-run` prints
   its prompt. `--learn` measures the posted pieces and rewrites the playbook
   when it is due, `--learn-now` rewrites it now, and `--learn --dry-run` prints
   what X says and the rewrite's prompt without changing anything. In
   `ops\config.yaml` the buttons are the manual steps `studio_now` (start a piece
   now), `studio_resume` (act on Continue, Revise and Resume), `studio_scan_now`
   (Scan now) and `studio_learn_now` (rewrite the playbook now). In every
   automatic run the `studio_scan` step runs `--scan` right before `studio`, and
   the `studio_learn` step runs `--learn` after the feedback snapshot it reads,
   each under a lock of its own so neither waits for a session.
12. Cost. One piece is one long session at max effort, often an hour, and it
    uses a lot of your Claude plan; one automatic piece a day is the shipped
    pace. Each stage has a time limit in `studio\config.yaml` (`timeouts:`).
13. What the session can do on your PC: search and read the web, and read
    and write files inside its own piece folder. The reference pieces reach it
    as a copy in that folder (`reference\`, about 2.5 MB, made when a stage
    starts), so nothing a web page talks it into can change
    `studio\exemplars\`, which every later piece reads.
    It has no shell, cannot touch anything else on the PC, ignores your
    CLAUDE.md, plugins and hooks, and cannot post (the `cli_flags` in
    `studio\config.yaml`).

The angles it chooses from (19 of them: deal decoder, class deep dive, catalyst
map, readout reaction, readout preview, the race, head to head, post-mortem,
regulatory decoder, follow the money, patent cliff, origin story, mechanism for
investors, contrarian take, bull vs bear, one chart, conference playbook, weekly
watchlist, scorecard) are in `studio\angles.yaml`. The app never offers an angle
the last three pieces used, and tells the session which hooks and openings to
avoid, so the account does not repeat itself. The card style (dark, 4:5, Inter)
is in `studio\brief\cards.md`, the voice in `studio\brief\voice.md`, and the three
reference pieces in `studio\exemplars\`.

---

## Fixing things

| Symptom | What to do |
|---|---|
| `Not logged in` or `OAuth session expired` in a score, draft or rate run | `claude login`, then rerun the command |
| An automatic run did not happen | the box at the top of `/runs` says why: off, the app was closed at that time (it is not made up), the computer slept more than an hour past it, its steps were still running from the last run, or another panel window runs the timer |
| `/runs` says a time was skipped | the reason is on the same line; press the run buttons to run it now |
| A run prints nothing for many minutes, then everything at once | the console was paused by a click (press Enter or Esc; untick "QuickEdit Mode" in the window's Properties) or the PC slept (`powercfg /change standby-timeout-ac 0`) |
| `git pull` refuses because you edited a file | `git checkout <file>` to discard, or ask me to commit the change |
| Scoring says `scoring 0 clusters` right after a keyword change | `python run_score.py --refilter` |
| Scoring says `pass: 0, deferred: N` | today's cap of 150 is spent; the N wait for tomorrow, or raise `daily_cap` in `config.yaml` |
| A source keeps erroring | it is logged and skipped; the others still run. Paste the line to me |
| A source says `Expecting value: line 1 column 1 (char 0)` | the API answered with an empty body. `api.biorxiv.org` does this (Sept 2026), so both preprint sources point at `api.medrxiv.org/details` in `config.yaml`, which serves the `biorxiv` and `medrxiv` servers alike |
| A company feed says `404 Not Found` | the company moved or retired its RSS feed. Open its press page, find the current feed and edit that company's `url` in `companies.feeds` in `config.yaml` (Cellectis, for one, now publishes at `/en/feed/?post_type=press_release`) |
| `clinicaltrials_oncology` says `403 Forbidden` | ClinicalTrials.gov blocks a Python program that calls itself a browser. Its entry in `config.yaml` has its own `user_agent` starting with `python-httpx/` for that reason; if the line was removed, put it back |
| A score run logs `safeguards flagged this message` | the CLI's usage-policy check tripped on a batch full of biology abstracts. The scorer does not retry the same prompt; it halves the batch and scores each half in a fresh call, down to single stories. A single story still refused is logged and left for the next run. `scorer_effort` in `config.yaml` set how hard the model thinks (`low` is the cheapest) |
| Score runs fail at once with `unknown option '--safe-mode'` | your Claude Code is older than the pipeline expects: `npm install -g @anthropic-ai/claude-code`, then run again. As a stopgap set `safe_mode: false` under `claude_code:` in `config.yaml` (your own CLAUDE.md and hooks then reach every call) |
| Want a completely fresh start | delete `pipeline.db`, then `python run_ingest.py --force` |
| The dashboard's `cli` check says `'claude' not found on PATH` | every model call runs the Claude Code CLI; install it and log in (part 1, step 3). If `claude` works in a Command Prompt but not from the app or a scheduled task, put its full path on `binary:` under `claude_code:` in `config.yaml` (`where claude` lists it; use the line ending in `claude.cmd`) |
| A step keeps running after you closed the app (a `claude` window keeps reopening) | that was the behaviour before the Stop button; on an old checkout, `taskkill /F /IM pythonw.exe` ends it (or `python.exe` if you started the app from a command prompt) |
| A draft sits on the approved page marked `claimed` and never posts | the run that claimed it died before it posted (a sleep, a power cut, the Stop button). Press "Release" beside it once the claim is over 30 minutes old, or run `python run_publish.py --release-failed`; check on the publishing page first that no part of it reached X |
| The control panel says a step is already running, or a run's step is marked `locked` | that step is running in this window, another window or the scheduler (part 6); wait for it and press the button again. Other steps can run meanwhile |
| The control panel will not start: `Address already in use` | another `run_app.py` or `run_queue.py` window is open; close it or use `--port 8001` |
| A studio piece says `no browser to draw the cards with` (its run's log: `no Chromium-family browser found`) | the cards are drawn by Edge, Chrome or Chromium; put the browser's full path on `render: browser:` in `studio\config.yaml` between single quotes (Edge is usually `browser: 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'`) and press Resume on the piece. The piece stops before polishing, its cards as written |
| A studio piece says `card card_1.html could not be drawn: a file could not be read or written` | another program has that card's picture open (an image viewer you opened from the piece's folder): close it; the next polish round draws the card again, and a piece that failed meanwhile has Resume |
| `run_ops.py run` never runs the studio | on purpose: a session runs an hour or more and would hold the whole run. Create the `pipeline-studio` task (part 6), which runs `run_ops.py run --only studio` |
| A studio piece fails at once with `unknown option '--restricted'` (or `--safe-mode`) | your Claude Code is older than the studio expects: `npm install -g @anthropic-ai/claude-code`, then Resume. As a stopgap remove that flag from `cli_flags` in `studio\config.yaml` |
| The radar page says the last scan `failed` | the line beside it says why (Claude Code not logged in, the time limit, an answer that was not JSON). Nothing was stored from it; press **Scan now**, or the next automatic run tries again. `radar: timeout_minutes` in `studio\config.yaml` gives a slow scan longer |
| The calendar shows one event twice with different dates | the date moved and both reports were kept. **Dismiss** the old one |
| The studio's performance page says `no snapshot yet` under a posted piece | the feedback step fetches numbers only with the X API read tier (part 7). Open **type the numbers X shows** under the piece and copy them from the post on x.com, about 48 hours after posting |
| The runs page shows `studio_learn` failed with `the rewritten playbook was not used` (or `the playbook rewrite call failed`) | nothing changed: the sessions keep the playbook they had, and the next run tries again. The log line says why (cut short, a section missing, Claude Code not logged in); **Rewrite the playbook now** on the performance page retries at once |
| A piece's page lists `the post reads like a report, not a person` | the checker counted too few sentences in the first person for the post's length and sent it back to the session; if the polish rounds ran out first it rides along as a note. Revise the piece ("add your own reactions") or edit it in the queue. To ask for fewer, raise `first_person_every_chars` under `voices:` in `studio\config.yaml` (0 turns the check off) |
| The playbook page says `not saved: the voice ...` | a heading in the Voices section is not `### key: Name` (a key is lower-case letters, digits and `_`), a key is listed twice, or a voice is empty or over 150 words. Nothing was saved; fix the line and save again |
| A learned playbook made the pieces worse | on the performance page, **put this version back** under the version you want; it becomes the newest version and the next session reads it. `learn: playbook: propose` in `studio\config.yaml` makes every rewrite wait for you instead |
| A studio piece says `interrupted` | the run stopped mid-stage (the Stop button, a stage time limit, the PC slept or restarted). Press Resume on its page; the session keeps everything it already read |
| A studio piece says `interrupted: the CLI stopped 1 sub-agent(s) before they finished` | Claude Code ended the cold fact-check before it reported (an old Claude Code, or one started with background tasks on), so the stage did not finish. Press Resume: the session runs the stage again, fact-check included |
| A studio piece still says `writing` (or researching, polishing, revising) after its run was stopped | another studio run holds that piece's writing slot (a `run_studio.py` in a command prompt, or a run in a second window): the piece belongs to it until it ends. Otherwise reload the page: with no studio run going, opening it marks the piece `interrupted` |
| A studio piece says `the piece's folder ... is missing` | its folder under `studio_pieces` was deleted or moved (moving the data folder moves them all). Put the folder back at that path and press Resume, or discard the piece |
| A studio piece's log says `the CLI no longer has session ...` | Claude Code cleaned up the piece's session (it keeps 30 days by default). Nothing to do: a fresh session took the piece over from its files and carried on |
| A studio piece says `failed: still blocked after polishing` | the page lists what the checker still found (advice wording, a link, a post over the limit). Resume with a note saying how to fix it, or Discard |
| `Pipeline.exe` opens and closes at once, or shows a blank window | read `desktop.log` next to it; a missing `.env` or a step that cannot start is logged there. A blank window means the Edge WebView2 runtime is missing: install it from Microsoft (it comes with Windows 11 and most Windows 10) |
| Building the exe fails with `No module named PyInstaller` | `pip install -e ".[desktop]"` in the venv first |

## Changing settings

Everything lives in `config.yaml` (sources, keywords, models, caps;
`claude_code:` is the Claude Code CLI every model call runs through, by name
or full path, with its time limit per call, and `safe_mode: true`, which keeps your
own Claude Code set-up (your CLAUDE.md, hooks, plugins) out of the pipeline's calls;
a source
can set its own `min_abstract_chars` when its feed only carries a one-line
summary, as the Fierce Biotech and BioPharma Dive entries do; `linking:` is the
story-linking pass that merges a release with the trade-press write-ups of it
before scoring, `enabled: false` turns it off),
`publish\config.yaml`
(posting slots, daily post cap, breaking-news rules; `media: attach_images`
attaches or skips the cards; posting itself is manual only), `feedback\config.yaml`,
`studio\config.yaml` (part 9: the studio's model and effort, how many
automatic pieces a day, whether they stop after research, stage time limits, the
session's tools, the card browser, `radar:` for the daily scan and the catalyst
calendar, `learn:` for what X teaches the next session: the horizon, the
baseline, when and how the playbook is rewritten, and `voices:` for the voices
the pieces are written in and how many of their sentences must speak in the
first person) and `ops\config.yaml` (which steps the
scheduler runs). Ask me to commit a change rather than editing by
hand, so your copy and GitHub stay in step.

### The clock you see

Every time and date on a screen — the control panel, the approval queue, the
digest, `run_ops.py status`, the health and feedback reports, the alert emails —
is shown in one time zone, set by `timezone:` near the top of `config.yaml`. It
ships as `America/Los_Angeles` (Seattle). To move it, edit that one line to
another zone name (`America/New_York`, `Europe/London`, and so on) and restart
whatever is running; nothing else changes.

The automatic runs' times (`auto_run_times` in `ops\config.yaml`) are in this
same zone, so 06:00 means 06:00 on the clock you see, before and after a daylight
saving change; restart the app after moving the zone.

Two other files have their own `timezone:` line, and you should set all three to
the same zone: `publish\config.yaml` (which decides what "9am" means for the
posting slots) and `feedback\config.yaml` (which decides what "hour posted"
means in the weekly report). Those two are about *behaviour*, not display, which
is why they are separate.

Behind the scenes nothing about the database moves: every timestamp is stored in
UTC and only translated when it is printed for you. A few things stay in UTC on
purpose, because they are filenames or keys rather than something to read: the
backup files in `backups\` (`pipeline-<UTC stamp>.sqlite`), the run ids on the
runs page, and the day a follower/metrics snapshot is filed under. Ages like
"3.2h ago" are the same in any zone.
