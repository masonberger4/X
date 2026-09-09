"""Step 5: operations layer (orchestrator, health checks, alerts, backups).

Nothing in this package posts to X, calls the Anthropic API, or edits pipeline
content. Other steps are run as subprocesses by their CLI names and their
tables are read-only to us.
"""
