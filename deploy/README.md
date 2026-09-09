# Deploying on a small VPS

```bash
sudo useradd -m pipeline && sudo -iu pipeline
git clone <repo-url> x && cd x
python3 -m venv .venv && .venv/bin/pip install -e .
cp .env.example .env && nano .env            # ANTHROPIC_API_KEY, optional ALERT_WEBHOOK_URL / SMTP_*
mkdir -p logs backups
.venv/bin/python run_ops.py run --dry-run    # shows the argv per step; runs nothing
.venv/bin/python run_ops.py run              # first real run (ingest -> score -> draft)
.venv/bin/python run_ops.py status
```

Then schedule it, either with cron (`crontab -e`, paste `deploy/crontab.example`,
fix `REPO`/`VENV`) or with systemd (`sudo cp deploy/pipeline.service deploy/pipeline.timer
/etc/systemd/system/ && sudo systemctl enable --now pipeline.timer`; add cron lines
for `health --alert`, `backup` and `prune` or copy the service/timer pattern).

Where things live (all relative to the repo root, set in `ops/config.yaml`):

- **Lock file** `pipeline.lock`. Overlapping runs exit 2. A stale lock from a crash
  is reclaimed automatically once its pid is gone.
- **Backups** `backups/pipeline-<UTC stamp>.sqlite`, verified with `PRAGMA
  integrity_check`, newest `backups.keep` kept.
- **Logs** `logs/ops.log` (cron) or `journalctl -u pipeline` (systemd). Step stdout/stderr
  tails are also stored in the `pipeline_runs` table; `run_ops.py status` shows the latest.
- **Health history** in `health_checks`; alert bookkeeping in `alerts_sent`.

Restore a backup (manual, on purpose): stop the timer/cron, `cp backups/pipeline-<stamp>.sqlite
pipeline.db`, run `run_ops.py status` to confirm, start the timer again.

Publishing is **off** and dry-run by default. Enable the `publish` step and add `--live` in
`ops/config.yaml`, and set `PUBLISH_ENABLED=1` in `.env`, only after reading README's
"Publishing (step 3)" section and confirming the bio disclosure.
