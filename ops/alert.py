"""Decide which health checks to notify about and send them.

Channels: log (always), webhook (JSON POST {"text": ...} to ALERT_WEBHOOK_URL; the only
outbound network call in ops/, in post_webhook so tests monkeypatch it), email (smtplib,
in send_email). Alert text contains check names, summaries and counts only: never
secrets, post text, or abstracts.
"""

from __future__ import annotations

import logging
import os
import smtplib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Any

from ops.models import STATUS_FAIL, STATUS_OK, STATUS_WARN, Check, Report

log = logging.getLogger(__name__)

KIND_ALERT = "alert"
KIND_RECOVERED = "recovered"
_RANK = {STATUS_WARN: 1, STATUS_FAIL: 2}


@dataclass
class Sent:
    check_name: str
    status: str  # status recorded: the check's status, or 'ok' for a recovery
    channel: str
    sent_at: datetime


@dataclass
class Due:
    check: Check
    kind: str  # KIND_ALERT | KIND_RECOVERED

    @property
    def record_status(self) -> str:
        return STATUS_OK if self.kind == KIND_RECOVERED else self.check.status


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


def due_checks(
    report: Report,
    prior: Mapping[str, tuple[str, datetime]],
    *,
    now: datetime,
    cooldown_hours: float,
    min_status: str = STATUS_WARN,
) -> list[Due]:
    """Which checks need a message: new/changed/expired problems, and recoveries."""
    cooldown = timedelta(hours=cooldown_hours)
    threshold = _RANK.get(min_status, 1)
    out: list[Due] = []
    for c in report.checks:
        last = prior.get(c.name)
        if c.status in _RANK and _RANK[c.status] >= threshold:
            if last is not None and last[0] == c.status and now - last[1] < cooldown:
                continue  # still failing the same way, inside the cooldown
            out.append(Due(c, KIND_ALERT))
        elif c.status == STATUS_OK and last is not None and last[0] in _RANK:
            out.append(Due(c, KIND_RECOVERED))
    return out


def format_message(report: Report, due: list[Due]) -> str:
    lines = [f"Pipeline health {report.overall.upper()} at {report.checked_at.isoformat()}"]
    for d in due:
        label = "RECOVERED" if d.kind == KIND_RECOVERED else d.check.status.upper()
        lines.append(f"{label} {d.check.name}: {d.check.summary}")
    return "\n".join(lines)


def format_subject(report: Report, due: list[Due]) -> str:
    names = ", ".join(d.check.name for d in due)
    return f"[pipeline] {report.overall.upper()}: {names}"


# ---------------------------------------------------------------------------
# Channels (each in one function so tests monkeypatch them)
# ---------------------------------------------------------------------------


def post_webhook(url: str, payload: dict[str, Any], timeout: float = 10) -> None:
    import httpx

    resp = httpx.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()


def send_email(
    *,
    host: str,
    port: int,
    user: str | None,
    password: str | None,
    sender: str,
    to: list[str],
    subject: str,
    body: str,
    timeout: float = 20,
) -> None:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(host, port, timeout=timeout) as smtp:
        smtp.ehlo()
        if port != 25:
            try:
                smtp.starttls()
                smtp.ehlo()
            except smtplib.SMTPNotSupportedError:
                pass
        if user and password:
            smtp.login(user, password)
        smtp.send_message(msg)


# ---------------------------------------------------------------------------
# notify
# ---------------------------------------------------------------------------


def log_report(report: Report) -> None:
    """The 'log' channel: every non-ok check, every time (WARNING for warn, ERROR for fail)."""
    for c in report.checks:
        if c.status == STATUS_WARN:
            log.warning("health %s: %s", c.name, c.summary)
        elif c.status == STATUS_FAIL:
            log.error("health %s: %s", c.name, c.summary)


def notify(
    report: Report,
    prior: Mapping[str, tuple[str, datetime]],
    cfg: dict[str, Any],
    *,
    now: datetime,
    env: Mapping[str, str] | None = None,
) -> list[Sent]:
    """Log the report, then send due checks to the enabled channels. Returns what was sent
    (one Sent per check per channel) so the caller can store it for the cooldown."""
    env = os.environ if env is None else env
    alerts = cfg.get("alerts") or {}
    channels = alerts.get("channels") or {}
    if channels.get("log", True):
        log_report(report)

    due = due_checks(
        report,
        prior,
        now=now,
        cooldown_hours=float(alerts.get("cooldown_hours", 6)),
        min_status=str(alerts.get("min_status", STATUS_WARN)),
    )
    if not due:
        return []
    text = format_message(report, due)
    sent: list[Sent] = []
    external_enabled = False

    if channels.get("webhook"):
        external_enabled = True
        url = env.get("ALERT_WEBHOOK_URL", "").strip()
        if not url:
            log.info("webhook channel enabled but ALERT_WEBHOOK_URL is not set; skipping")
        else:
            try:
                post_webhook(url, {"text": text}, float(alerts.get("webhook_timeout_seconds", 10)))
                sent.extend(Sent(d.check.name, d.record_status, "webhook", now) for d in due)
                log.info("webhook alert sent for %s", ", ".join(d.check.name for d in due))
            except Exception as exc:
                log.error("webhook alert failed: %s", type(exc).__name__)

    if channels.get("email"):
        external_enabled = True
        host = env.get("SMTP_HOST", "").strip()
        sender = env.get("ALERT_EMAIL_FROM", "").strip()
        to = [a.strip() for a in env.get("ALERT_EMAIL_TO", "").split(",") if a.strip()]
        if not (host and sender and to):
            log.info("email channel enabled but SMTP_HOST/ALERT_EMAIL_FROM/TO not set; skipping")
        else:
            try:
                send_email(
                    host=host,
                    port=int(env.get("SMTP_PORT") or 587),
                    user=env.get("SMTP_USER") or None,
                    password=env.get("SMTP_PASSWORD") or None,
                    sender=sender,
                    to=to,
                    subject=format_subject(report, due),
                    body=text,
                )
                sent.extend(Sent(d.check.name, d.record_status, "email", now) for d in due)
                log.info("email alert sent for %s", ", ".join(d.check.name for d in due))
            except Exception as exc:
                log.error("email alert failed: %s", type(exc).__name__)

    if not external_enabled:
        # Only the log channel: record it so the cooldown still applies to log noise.
        sent.extend(Sent(d.check.name, d.record_status, "log", now) for d in due)
    return sent
