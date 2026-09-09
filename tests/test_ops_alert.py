"""ops/alert.py with monkeypatched post_webhook / send_email; never hits the network."""

import json
import logging
import os
from datetime import UTC, datetime, timedelta

import pytest

from ops import alert
from ops.models import Check, Report

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
SENTINEL = "sekrit-webhook-token-9f8e7d"


def cfg(**channels):
    ch = {"log": True, "webhook": True, "email": False}
    ch.update(channels)
    return {"alerts": {"cooldown_hours": 6, "min_status": "warn", "channels": ch}}


def report(status: str, name="sources", when=NOW) -> Report:
    return Report.build(when, [Check(name, status, f"{name} summary"), Check("ok1", "ok", "")])


@pytest.fixture
def hook(monkeypatch):
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        alert, "post_webhook", lambda url, payload, timeout=10: calls.append((url, payload))
    )
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example/abc")
    return calls


def test_fail_sends_once_then_cooldown_then_again(hook):
    prior: dict = {}
    sent = alert.notify(report("fail"), prior, cfg(), now=NOW)
    assert len(hook) == 1
    assert [(s.check_name, s.status, s.channel) for s in sent] == [("sources", "fail", "webhook")]
    prior = {s.check_name: (s.status, s.sent_at) for s in sent}

    # Same failure 1h later: inside the cooldown, nothing sent.
    later = NOW + timedelta(hours=1)
    assert alert.notify(report("fail", when=later), prior, cfg(), now=later) == []
    assert len(hook) == 1

    # After the cooldown it is sent again.
    much_later = NOW + timedelta(hours=7)
    sent = alert.notify(report("fail", when=much_later), prior, cfg(), now=much_later)
    assert len(hook) == 2 and sent[0].sent_at == much_later


def test_status_change_inside_cooldown_sends(hook):
    prior = {"sources": ("warn", NOW - timedelta(hours=1))}
    alert.notify(report("fail"), prior, cfg(), now=NOW)
    assert len(hook) == 1 and "FAIL sources" in hook[0][1]["text"]


def test_recovery_sends_one_message(hook):
    prior = {"sources": ("fail", NOW - timedelta(hours=1))}
    sent = alert.notify(report("ok"), prior, cfg(), now=NOW)
    assert len(hook) == 1
    assert "RECOVERED sources" in hook[0][1]["text"]
    assert sent[0].status == "ok"
    # Once recorded as ok, a still-ok check sends nothing more.
    prior = {"sources": ("ok", NOW)}
    assert alert.notify(report("ok"), prior, cfg(), now=NOW + timedelta(hours=1)) == []
    assert len(hook) == 1


def test_ok_with_no_prior_sends_nothing(hook):
    assert alert.notify(report("ok"), {}, cfg(), now=NOW) == []
    assert hook == []


def test_skip_is_never_alerted(hook):
    assert alert.notify(report("skip"), {}, cfg(), now=NOW) == []


def test_min_status_fail_suppresses_warn(hook):
    c = cfg()
    c["alerts"]["min_status"] = "fail"
    assert alert.notify(report("warn"), {}, c, now=NOW) == []
    assert alert.notify(report("fail"), {}, c, now=NOW)


def test_payload_never_contains_env_values(hook, monkeypatch):
    monkeypatch.setenv("OPS_TEST_SECRET", SENTINEL)
    monkeypatch.setenv("ANTHROPIC_API_KEY", SENTINEL + "-anthropic")
    rep = Report.build(
        NOW, [Check("env", "fail", "missing env: X_API_KEY"), Check("s", "warn", "w")]
    )
    alert.notify(rep, {}, cfg(), now=NOW)
    payload = json.dumps(hook[0][1])
    assert SENTINEL not in payload
    for value in os.environ.values():
        if len(value) >= 8:
            assert value not in payload
    assert hook[0][0] == "https://hooks.example/abc"
    assert "missing env: X_API_KEY" in payload  # names are fine, values never


def test_missing_webhook_url_is_skipped_with_info(monkeypatch, caplog):
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(alert, "post_webhook", lambda *a, **k: pytest.fail("must not be called"))
    with caplog.at_level(logging.INFO, logger="ops.alert"):
        sent = alert.notify(report("fail"), {}, cfg(), now=NOW)
    assert sent == []
    assert any("ALERT_WEBHOOK_URL is not set" in r.message for r in caplog.records)
    assert not any(r.levelno >= logging.ERROR and "webhook" in r.message for r in caplog.records)


def test_webhook_error_is_logged_not_raised(monkeypatch, caplog):
    def boom(*a, **k):
        raise ConnectionError("down")

    monkeypatch.setattr(alert, "post_webhook", boom)
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example/abc")
    with caplog.at_level(logging.ERROR, logger="ops.alert"):
        sent = alert.notify(report("fail"), {}, cfg(), now=NOW)
    assert sent == []  # not recorded, so it is retried next run
    assert any("webhook alert failed" in r.message for r in caplog.records)


def test_email_channel(monkeypatch):
    mails: list[dict] = []
    monkeypatch.setattr(alert, "send_email", lambda **kw: mails.append(kw))
    env = {
        "SMTP_HOST": "smtp.example",
        "SMTP_PORT": "2525",
        "SMTP_USER": "u",
        "SMTP_PASSWORD": "p",
        "ALERT_EMAIL_FROM": "ops@example",
        "ALERT_EMAIL_TO": "a@example, b@example",
    }
    sent = alert.notify(report("fail"), {}, cfg(webhook=False, email=True), now=NOW, env=env)
    assert [s.channel for s in sent] == ["email"]
    assert mails[0]["to"] == ["a@example", "b@example"]
    assert mails[0]["port"] == 2525
    assert mails[0]["subject"].startswith("[pipeline] FAIL: sources")
    assert "p" not in mails[0]["body"].split()  # password never in the body


def test_missing_smtp_is_skipped_with_info(monkeypatch, caplog):
    monkeypatch.setattr(alert, "send_email", lambda **kw: pytest.fail("must not be called"))
    with caplog.at_level(logging.INFO, logger="ops.alert"):
        sent = alert.notify(report("fail"), {}, cfg(webhook=False, email=True), now=NOW, env={})
    assert sent == []
    assert any("SMTP_HOST" in r.message and r.levelno == logging.INFO for r in caplog.records)


def test_log_only_records_log_channel(caplog):
    with caplog.at_level(logging.WARNING, logger="ops.alert"):
        sent = alert.notify(report("warn"), {}, cfg(webhook=False), now=NOW)
    assert [s.channel for s in sent] == ["log"]
    assert any(r.levelno == logging.WARNING and "sources" in r.message for r in caplog.records)
    with caplog.at_level(logging.ERROR, logger="ops.alert"):
        alert.notify(report("fail"), {}, cfg(webhook=False), now=NOW)
    assert any(r.levelno == logging.ERROR and "sources" in r.message for r in caplog.records)


def test_format_message_lists_only_due_checks():
    rep = Report.build(NOW, [Check("a", "fail", "A bad"), Check("b", "warn", "B meh")])
    due = alert.due_checks(rep, {}, now=NOW, cooldown_hours=6)
    text = alert.format_message(rep, due)
    assert text.splitlines()[0].startswith("Pipeline health FAIL")
    assert "FAIL a: A bad" in text and "WARN b: B meh" in text
