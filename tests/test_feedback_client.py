"""feedback/client.py: parsing against saved fixtures and the retry policy. No network."""

import json
import logging
from pathlib import Path

import httpx
import pytest

from feedback import client

FIXTURES = Path(__file__).parent / "fixtures"
TWEETS = json.loads((FIXTURES / "x_tweets_lookup.json").read_text())
USER = json.loads((FIXTURES / "x_user_lookup.json").read_text())
IDS = ["1801234567890123456", "1801234567890123457", "1801234567890123999"]


def test_parse_tweets_fixture_marks_missing_as_deleted():
    got = client.parse_tweets_response(TWEETS, IDS)
    assert set(got) == set(IDS)
    head = got["1801234567890123456"]
    assert not head.deleted
    assert head.metrics.impressions == 4821 and head.metrics.likes == 58
    assert head.metrics.reposts == 12 and head.metrics.replies == 3
    assert head.metrics.quotes == 2 and head.metrics.bookmarks == 9
    assert head.created_at.isoformat() == "2026-06-01T12:30:00+00:00"
    gone = got["1801234567890123999"]
    assert gone.deleted and gone.metrics.impressions == 0


def test_parse_tweets_absent_without_error_entry_is_still_deleted():
    got = client.parse_tweets_response({"data": []}, ["1"])
    assert got["1"].deleted


def test_parse_user_fixture():
    um = client.parse_user_response(USER, "oncwatch")
    assert (um.username, um.followers, um.following, um.tweet_count) == ("oncwatch", 412, 88, 131)
    with pytest.raises(client.FeedbackAPIError):
        client.parse_user_response({"errors": [{"title": "Not Found Error"}]}, "nobody")


class FakeTransport:
    """Replaces httpx.Client with a scripted sequence of (status, body, headers)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, *a, **kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None, headers=None):
        self.requests.append((url, params, headers))
        status, body, hdrs = self.responses.pop(0)
        if isinstance(body, Exception):
            raise body
        req = httpx.Request("GET", url)
        return httpx.Response(status, json=body, headers=hdrs, request=req)


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "secret-token-xyz")
    sleeps = []
    monkeypatch.setattr(client, "_sleep", sleeps.append)

    def install(responses):
        t = FakeTransport(responses)
        monkeypatch.setattr(client.httpx, "Client", t)
        t.sleeps = sleeps
        return t

    return install


CFG = {"rate_limit": {"max_attempts": 3, "backoff_seconds": 1, "max_wait_seconds": 60}}


def test_get_tweet_metrics_batches_and_sends_bearer(fake, monkeypatch):
    t = fake([(200, TWEETS, {}), (200, {"data": []}, {})])
    ids = IDS + [str(i) for i in range(100, 200)]  # 103 ids -> 2 calls
    got = client.get_tweet_metrics(ids, {"api_base": "https://api.x.com/2/"})
    assert len(t.requests) == 2
    url, params, headers = t.requests[0]
    assert url == "https://api.x.com/2/tweets"
    assert headers["Authorization"] == "Bearer secret-token-xyz"
    assert params["tweet.fields"] == "public_metrics,created_at"
    assert len(params["ids"].split(",")) == 100
    assert len(got) == 103 and got["150"].deleted and not got[IDS[0]].deleted


def test_missing_token_raises_before_any_request(fake, monkeypatch):
    fake([])
    monkeypatch.delenv("X_BEARER_TOKEN")
    with pytest.raises(client.FeedbackAPIError, match="X_BEARER_TOKEN"):
        client.get_tweet_metrics(["1"])


def test_429_honours_reset_header_capped_then_succeeds(fake, monkeypatch):
    monkeypatch.setattr(client.time, "time", lambda: 1000.0)
    t = fake(
        [
            (429, {}, {"x-rate-limit-reset": "1030"}),  # 31s
            (429, {}, {"x-rate-limit-reset": "5000"}),  # capped at 60
            (200, USER, {}),
        ]
    )
    um = client.get_user_metrics("@oncwatch", CFG)
    assert um.followers == 412
    assert t.sleeps == [31.0, 60.0]
    assert t.requests[0][0].endswith("/users/by/username/oncwatch")


def test_5xx_backs_off_then_gives_up(fake):
    t = fake([(500, {}, {}), (503, {}, {}), (502, {}, {})])
    with pytest.raises(client.FeedbackAPIError, match="gave up after 3") as exc:
        client.get_tweet_metrics(["1"], CFG)
    assert exc.value.retryable and t.sleeps == [1.0, 2.0, 4.0]


@pytest.mark.parametrize("status", [401, 403])
def test_401_403_never_retried(fake, status):
    t = fake([(status, {}, {}), (200, TWEETS, {})])
    with pytest.raises(client.FeedbackAPIError) as exc:
        client.get_tweet_metrics(["1"], CFG)
    assert exc.value.status == status and len(t.requests) == 1 and t.sleeps == []


def test_network_error_is_retried(fake):
    t = fake([(0, httpx.ConnectError("boom"), {}), (200, USER, {})])
    assert client.get_user_metrics("oncwatch", CFG).followers == 412
    assert len(t.requests) == 2


def test_token_never_logged(fake, caplog):
    fake([(429, {}, {}), (401, {}, {})])
    with caplog.at_level(logging.DEBUG), pytest.raises(client.FeedbackAPIError):
        client.get_tweet_metrics(["1"], CFG)
    assert "secret-token-xyz" not in caplog.text
    assert "not retrying" in caplog.text
