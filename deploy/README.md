# Deploying on a small VPS

```bash
sudo useradd -m pipeline && sudo -iu pipeline
git clone <repo-url> x && cd x
python3 -m venv .venv && .venv/bin/pip install -e .
npm config set prefix ~/.npm-global && npm install -g @anthropic-ai/claude-code
echo 'export PATH="$HOME/.npm-global/bin:$PATH"' >> ~/.profile && . ~/.profile
claude login                                 # every model call runs this CLI, as this user
cp .env.example .env && nano .env            # optional ALERT_WEBHOOK_URL / SMTP_*; no Claude key
mkdir -p logs backups
.venv/bin/python run_ops.py run --dry-run    # shows the argv per step; runs nothing
.venv/bin/python run_ops.py run              # first real run (ingest -> score -> draft)
.venv/bin/python run_ops.py status
```

Every model call (scoring, linking, drafting, grading, claim checks) runs the Claude
Code CLI (Node.js needed for the npm install) on the login of the user that runs the
pipeline, so log in as `pipeline` as above. There is no API key: an `ANTHROPIC_API_KEY`
in `.env` is ignored, kept out of the CLI's environment. cron and systemd start with a
short PATH that misses the npm folder: add `/home/pipeline/.npm-global/bin` to the
`PATH=` line in `deploy/crontab.example` or uncomment the `Environment=PATH=` line in
`deploy/pipeline.service`, or set `claude_code.binary` in `config.yaml` to the full
path. `run_ops.py health` has a `cli` check that fails while the CLI cannot be found.

Then schedule it, either with cron (`crontab -e`, paste `deploy/crontab.example`,
fix `REPO`/`VENV`/`PATH`) or with systemd (`sudo cp deploy/pipeline.service deploy/pipeline.timer
deploy/pipeline-studio.service deploy/pipeline-studio.timer /etc/systemd/system/ && sudo
systemctl enable --now pipeline.timer pipeline-studio.timer`; add cron lines for
`health --alert`, `backup` and `prune` or copy the service/timer pattern).

The studio (step 10) has an entry of its own, `run_ops.py run --only studio`, three times a
day (the crontab's `10 6,12,18` line, or `pipeline-studio.timer`), and a plain
`run_ops.py run` leaves it out. One session is research, writing and a fact-check, an hour
or more: inside the plain run it would hold back every step after it, and the next fires
would exit 2 on the run lock (or, under systemd, not start at all while the oneshot unit is
still running). Its own entry takes only the studio's lock, so the pipeline keeps running
beside it; the plain run's draft step leaves alone every story the session was offered while
it researches, so the two never write the same story. `pipeline-studio.service` has no unit time limit (`TimeoutStartSec=infinity`):
each stage has its own in `studio/config.yaml`, and a unit limit would kill a session
mid-stage. Keep `pipeline.service`'s one hour for the plain run.

Where things live (all relative to the repo root, set in `ops/config.yaml`):

- **Lock file** `pipeline.lock`. Overlapping runs exit 2. A stale lock from a crash
  is reclaimed automatically once its pid is gone.
- **Backups** `backups/pipeline-<UTC stamp>.sqlite`, verified with `PRAGMA
  integrity_check`, newest `backups.keep` kept, each with a copy of the files named in
  `backups.with_db` beside it (`pipeline-<UTC stamp>.studio_playbook.md`: the studio's
  playbook, which lives only beside the database).
- **Studio** `studio_pieces/` (one folder per piece: fact base, posts, cards, fact-check
  log, the session's transcript) and `studio_playbook.md`, beside the database. Backups
  carry the playbook, not the pieces: copy `studio_pieces/` yourself when you move the
  data.
- **Logs** `logs/ops.log` (cron) or `journalctl -u pipeline` (systemd). Step stdout/stderr
  tails are also stored in the `pipeline_runs` table; `run_ops.py status` shows the latest.
- **Health history** in `health_checks`; alert bookkeeping in `alerts_sent`.

Restore a backup (manual, on purpose): stop the timer/cron, `cp backups/pipeline-<stamp>.sqlite
pipeline.db` (and `cp backups/pipeline-<stamp>.studio_playbook.md studio_playbook.md` when
there is one), run `run_ops.py status` to confirm, start the timer again.

The scheduled `publish` step is a dry run and stays one: posting is **manual only**.
`run_ops.py run` refuses to start when any step in `ops/config.yaml` carries `--live`, and
the control panel has no automatic publisher. A post goes out only when a human presses
"Publish now" on the approved page (or runs `run_publish.py --live` by hand), with
`PUBLISH_ENABLED=1` in `.env`; read README's "Publishing (step 3)" section and confirm the
bio disclosure first.

## Windows (Task Scheduler)

Cron and systemd do not exist on Windows; `run_ops.py` itself works there. Create
four scheduled tasks from an Administrator command prompt, with `C:\Users\you\X`
replaced by your checkout (and `python` by `.venv\Scripts\python.exe` if you use a
virtual environment):

```bat
schtasks /Create /TN "pipeline-run"    /SC MINUTE /MO 30 /TR "cmd /c cd /d C:\Users\you\X && python run_ops.py run >> logs\ops.log 2>&1"
schtasks /Create /TN "pipeline-health" /SC HOURLY        /TR "cmd /c cd /d C:\Users\you\X && python run_ops.py health --alert >> logs\ops.log 2>&1"
schtasks /Create /TN "pipeline-backup" /SC DAILY /ST 03:00 /TR "cmd /c cd /d C:\Users\you\X && python run_ops.py backup >> logs\ops.log 2>&1"
schtasks /Create /TN "pipeline-studio" /SC DAILY /ST 06:10 /RI 360 /DU 12:30 /TR "cmd /c cd /d C:\Users\you\X && python run_ops.py run --only studio >> logs\ops.log 2>&1"
```

`pipeline-studio` runs the studio at 06:10, 12:10 and 18:10 (see the studio paragraph
above: the plain `pipeline-run` leaves it out, and Task Scheduler never starts a task that
is still running, so a session inside `pipeline-run` would stop the pipeline for an hour or
more).

Create the `logs` folder first (`mkdir logs`). The PC must be awake for tasks to
fire; in Task Scheduler's GUI, tick "Run whether user is logged on or not" and
"Wake the computer to run this task" for each. `schtasks /Query /TN pipeline-run`
shows the next run; `schtasks /Delete /TN pipeline-run` removes one. Every model
call runs the Claude Code CLI, so the tasks must run as the Windows user that ran
`claude login`.

## Desktop build (Windows)

`desktop.spec` is the PyInstaller spec for the control panel as a double-click app.
From the repo root with the venv active:

```
pip install -e ".[desktop]"
pyinstaller deploy\desktop.spec
```

`dist\Pipeline\` then holds `Pipeline.exe` (the window, no console), `pipeline-cli.exe`
(a console build the runs page launches steps with; it maps `run_ingest.py` and the
other script names to their modules) and `_internal\` with the code and every settings
file at its usual relative path. `.env`, `pipeline.db`, `backups\`, `images\`,
`studio_pieces\`, `studio_playbook.md`, the lock and `desktop.log` live beside the exe:
rebuilding into `dist\Pipeline` deletes them, so keep the app outside `dist\` or copy them
out first (HOWTO part 8). Rebuild after every code change; the exe does not
update itself. HOWTO part 8 has the operator's version.
