"""Step 8: the control panel — one web app over the whole pipeline.

`panel/` is a presentation layer. It owns no tables and no pipeline logic: every
number it shows comes from `ops/store.py`'s read-only adapters and the pure checks in
`ops/health.py`, and every step it runs is executed as a subprocess by `ops/runner.py`
under `ops/lock.py`, exactly as cron does. It composes the step 2 approval queue's
routes into the same app so the operator has a single URL.
"""
