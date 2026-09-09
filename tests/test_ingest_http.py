"""Retry/backoff in ingest/http.py (step 6). httpx.get and time.sleep are monkeypatched."""

import logging

import httpx
import pytest

from ingest import http

URL = "https://api.example.org/works"


def _resp(status: int, headers: dict | None = None, body: str = '{"ok": true}') -> httpx.Response:
    req = httpx.Request("GET", URL)
    return httpx.Response(status, headers=headers or {}, content=body.encode(), request=req)


class FakeGet:
    """Returns/raises a scripted sequence and records every call."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, url, *, params=None, headers=None, timeout=None, follow_redirects=None):
        self.calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        nxt = self.script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


@pytest.fixture
def sleeps(monkeypatch):
    waits = []
    monkeypatch.setattr(http.time, "sleep", lambda s: waits.append(s))
    return waits


def test_503_then_200_succeeds_in_two_calls(monkeypatch, sleeps):
    fake = FakeGet([_resp(503), _resp(200)])
    monkeypatch.setattr(http.httpx, "get", fake)
    assert http.get_json(URL) == {"ok": True}
    assert len(fake.calls) == 2
    assert sleeps == [http.BACKOFF_BASE_SECONDS]


def test_429_honours_numeric_retry_after(monkeypatch, sleeps):
    fake = FakeGet([_resp(429, {"Retry-After": "7"}), _resp(200, body="hello")])
    monkeypatch.setattr(http.httpx, "get", fake)
    assert http.get_text(URL) == "hello"
    assert sleeps == [7.0]


def test_retry_after_is_capped(monkeypatch, sleeps):
    fake = FakeGet([_resp(429, {"Retry-After": "99999"}), _resp(200)])
    monkeypatch.setattr(http.httpx, "get", fake)
    http.get_json(URL)
    assert sleeps == [http.MAX_RETRY_WAIT]


def test_404_raises_without_retry(monkeypatch, sleeps):
    fake = FakeGet([_resp(404), _resp(200)])
    monkeypatch.setattr(http.httpx, "get", fake)
    with pytest.raises(httpx.HTTPStatusError) as ei:
        http.get_json(URL)
    assert ei.value.response.status_code == 404
    assert len(fake.calls) == 1
    assert sleeps == []


def test_max_attempts_failures_raise_last_error(monkeypatch, sleeps):
    fake = FakeGet([_resp(500), _resp(502), _resp(503), _resp(200)])
    monkeypatch.setattr(http.httpx, "get", fake)
    with pytest.raises(httpx.HTTPStatusError) as ei:
        http.get_json(URL, max_attempts=3)
    assert ei.value.response.status_code == 503
    assert len(fake.calls) == 3
    assert sleeps == [1.0, 2.0]


def test_transport_errors_retry_then_raise(monkeypatch, sleeps):
    req = httpx.Request("GET", URL)
    fake = FakeGet([httpx.ConnectError("boom", request=req), httpx.ReadTimeout("t", request=req)])
    monkeypatch.setattr(http.httpx, "get", fake)
    with pytest.raises(httpx.ReadTimeout):
        http.get_json(URL, max_attempts=2)
    assert len(fake.calls) == 2


def test_transport_error_then_success(monkeypatch, sleeps):
    req = httpx.Request("GET", URL)
    fake = FakeGet([httpx.ConnectError("boom", request=req), _resp(200)])
    monkeypatch.setattr(http.httpx, "get", fake)
    assert http.get_json(URL) == {"ok": True}


def test_headers_reach_httpx_and_never_hit_the_log(monkeypatch, sleeps, caplog):
    fake = FakeGet([_resp(200)])
    monkeypatch.setattr(http.httpx, "get", fake)
    token = "SECRET-BEARER-VALUE"
    with caplog.at_level(logging.DEBUG, logger="ingest.http"):
        http.get_json(
            URL,
            params={"q": 1},
            user_agent="ua-test",
            headers={"Authorization": f"Bearer {token}"},
        )
    sent = fake.calls[0]["headers"]
    assert sent["Authorization"] == f"Bearer {token}"
    assert sent["User-Agent"] == "ua-test"
    assert caplog.text  # the DEBUG line was emitted...
    assert URL in caplog.text and "'q': 1" in caplog.text
    assert token not in caplog.text and "Authorization" not in caplog.text


def test_default_signature_unchanged(monkeypatch, sleeps):
    """Existing callers pass only url/params/user_agent/timeout."""
    fake = FakeGet([_resp(200, body="<rss/>")])
    monkeypatch.setattr(http.httpx, "get", fake)
    assert http.get_text(URL, params=None, user_agent=None, timeout=5.0) == "<rss/>"
    assert fake.calls[0]["timeout"] == 5.0
    assert fake.calls[0]["headers"] == {"User-Agent": "cancer-news-pipeline/0.1"}
